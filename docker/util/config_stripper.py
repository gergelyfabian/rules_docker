#!/usr/bin/python

# Copyright 2017 The Bazel Authors. All rights reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

# http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import io
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading

_TIMESTAMP = '1970-01-01T00:00:00Z'

WHITELISTED_PREFIXES = ['sha256:', 'manifest', 'repositories']

_BUF_SIZE = 4096

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--in_tar_path', type=str,
                        help='Path to docker save tarball',
                        required=True)
    parser.add_argument('--out_tar_path', type=str,
                        help='Path to output stripped tarball',
                        required=True)
    args = parser.parse_args()

    os.environ["PYTHONIOENCODING"] = "utf-8"

    return strip_tar(args.in_tar_path, args.out_tar_path)


def strip_tar(input, output):
    # Unpack the tarball, modify configs in place, and rearchive.
    # We need to take care to keep the files sorted.

    tempdir = tempfile.mkdtemp()
    with tarfile.open(name=input, mode='r') as it:
        it.extractall(tempdir)

    mf_path = os.path.join(tempdir, 'manifest.json')
    with open(mf_path, 'r') as mf:
        manifest = json.load(mf)
    for image in manifest:
        # Scrape each layer for any timestamps
        new_layers = []
        new_diff_ids = []
        # Process the layers in reverse to fix https://github.com/bazelbuild/rules_docker/issues/1104.
        # Image tarballs generated by "docker save" can include layer tarballs
        # that are just symlinks to the previous layer. In the Docker manifest,
        # the layers are ordered from top to bottom. Thus, as the stripper goes
        # through these layers in top to bottom order, it may encounter a layer
        # that symlinks to a lower layer that hasn't been extracted yet. Just
        # reversing the iteration order avoids this problem.
        for layer in reversed(image['Layers']):
          (new_layer_name, new_diff_id) = strip_layer(os.path.join(tempdir, layer))

          new_layers.append(new_layer_name)
          new_diff_ids.append(new_diff_id)
        # Reverse the layer order to go back to the topmost layer to bottom as
        # required by the Docker manifest format.
        new_layers = [l for l in reversed(new_layers)]
        new_diff_ids = [d for d in reversed(new_diff_ids)]

        # Change the manifest to reflect the new layer name
        image['Layers'] = new_layers

        config = image['Config']
        cfg_path = os.path.join(tempdir, config)
        new_cfg_path = strip_config(cfg_path, new_diff_ids)

        # Update the name of the config in the metadata object
        # to match it's new digest.
        image['Config'] = new_cfg_path

    # Rewrite the manifest with the new config names.
    with open(mf_path, 'w') as f:
        json.dump(manifest, f, sort_keys=True)

    # Rewrite the legacy repositories file with the new layer name.
    repositories_path = os.path.join(tempdir, 'repositories')
    first_manifest_entry = next(iter(manifest), None)
    if first_manifest_entry is not None:
        repo_tags = first_manifest_entry['RepoTags'][0]
        repo, tag = repo_tags.split(":")
        last_layer = first_manifest_entry['Layers'][-1]
        repositories = {repo: {tag: last_layer}}

        # Rewrite repositories with the new layer name.
        with open(repositories_path, 'w') as f:
            json.dump(repositories, f, sort_keys=True)

    # Collect the files before adding, so we can sort them.
    files_to_add = []
    for root, _, files in os.walk(tempdir):
        for f in files:
            if os.path.basename(f).startswith(tuple(WHITELISTED_PREFIXES)):
                name = os.path.join(root, f)
                os.utime(name, (0,0))
                files_to_add.append(name)

    with tarfile.open(name=output, mode='w') as ot:
        for f in sorted(files_to_add):
            # Strip the tempdir path
            arcname = os.path.relpath(f, tempdir)
            ot.add(f, arcname)

    shutil.rmtree(tempdir)
    return 0

def strip_layer(path):
    # The original layer tar is of the form <random string>/layer.tar, the
    # working directory is one level up from where layer.tar is.
    original_dir = os.path.normpath(os.path.join(os.path.dirname(path), '..'))

    # Write compressed tar to a temporary name. We'll rename it to the correct
    # name after we compute the hash.
    gz_out = tempfile.NamedTemporaryFile(dir=original_dir, delete=False)

    # Keep track of sha hash for both the compressed and uncompressed tar
    uncompressed_sha = hashlib.sha256()
    compressed_sha = hashlib.sha256()

    # Start a gzip process that we'll use to compress tar output.
    # Shelling out to bash gzip is noticeably faster than using python's gzip
    #
    # This function takes special care to never store the full tar file or
    # gzip'd tar file in memory. Images can be quite large.
    gzip_process = subprocess.Popen(
        ['gzip', '-nf'],
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE)

    # Read the gzip'd output and accumulate the sha hash, and save the
    # compressed copy under the new name.
    gzip_stdout_exc = []
    def do_gzip_stdout():
        try:
            while True:
                buf = gzip_process.stdout.read(_BUF_SIZE)
                if not buf: break
                compressed_sha.update(buf)
                gz_out.write(buf)
        except Exception as e:
            gzip_stdout_exc.append(e)

    # Read the gzip stderr for error reporting.
    gzip_stderr_buf = io.BytesIO()
    def do_gzip_stderr():
        # Don't bother incrementally reading stderr.
        gzip_stderr_buf.write(gzip_process.stderr.read())

    # Start all of the threads to prepare for producing the tar file.
    gzip_stdout = threading.Thread(target=do_gzip_stdout)
    gzip_stdout.start()
    gzip_stderr = threading.Thread(target=do_gzip_stderr)
    gzip_stderr.start()
    try:
        # Go through each file/dir in the layer
        # Set its mtime to 0
        # If it's a file, add its content to the running buffer
        # Add it to the new gzip'd tar.
        with tempfile.TemporaryFile() as t:
            with tarfile.open(name=path, mode='r') as it:
                with tarfile.open(fileobj=t, encoding='utf-8', mode='w') as ot:
                    for tarinfo in it:
                        # Use a deterministic mtime that doesn't confuse other
                        # programs,  e.g. Python.  Also see
                        # https://github.com/bazelbuild/bazel/issues/1299
                        tarinfo.mtime = 946684800 # 2000-01-01 00:00:00.000 UTC
                        if tarinfo.isfile():
                            f = it.extractfile(tarinfo)
                            ot.addfile(tarinfo, f)
                        else:
                            ot.addfile(tarinfo)

            # Read the stripped tarfile. Accumulate a hash of the uncompressed
            # file and send data on to the gzip process for compression.
            t.seek(0)
            while True:
                buf = t.read(_BUF_SIZE)
                if not buf: break
                uncompressed_sha.update(buf)
                gzip_process.stdin.write(buf)
    finally:
        gzip_process.stdin.close() # Causes gzip to terminate.
        gzip_stdout.join() # Terminates after gzip closes stdout.
        gzip_stderr.join() # Terminates after gzip closes stderr.
        gzip_process.wait() # gzip terminated by now.

    # Check if any of our threads or processes failed.
    if gzip_stdout_exc:
        raise gzip_stdout_exc[0]
    if gzip_process.returncode != 0:
        raise RuntimeError(
                'Failed to gzip stripped layer %s. '
                'gzip exited with status %d: %s',
                path, gzip_process.returncode, gzip_stderr_buf.getvalue())

    # Create the new diff_id for the config
    diffid = 'sha256:%s' % uncompressed_sha.hexdigest()

    # Rename into correct location now that we know the hash.
    new_name = 'sha256:%s' % compressed_sha.hexdigest()
    os.rename(gz_out.name, os.path.join(original_dir, new_name))

    shutil.rmtree(os.path.dirname(path))
    return (new_name, diffid)


def strip_config(path, new_diff_ids):
    with open(path, 'r') as f:
        config = json.load(f)
    config['created'] = _TIMESTAMP
    config['rootfs']['diff_ids'] = new_diff_ids

    # Base container info is not required and changes every build, so delete it.
    if 'container' in config:
      del config['container']
    if ('config' in config and
        'Hostname' in config['config']):
      del config['config']['Hostname']
    if ('container_config' in config and
        'Hostname' in config['container_config']):
      del config['container_config']['Hostname']
    if 'docker_version' in config:
      del config['docker_version']
    for entry in config['history']:
        entry['created'] = _TIMESTAMP

    config_str = json.dumps(config, sort_keys=True)
    with open(path, 'w') as f:
        f.write(config_str)

    # Calculate the new file path
    sha = hashlib.sha256(config_str.encode("utf-8")).hexdigest()
    new_path = 'sha256:%s' % sha
    os.rename(path, os.path.join(os.path.dirname(path), new_path))
    return new_path


if __name__ == "__main__":
    sys.exit(main())
