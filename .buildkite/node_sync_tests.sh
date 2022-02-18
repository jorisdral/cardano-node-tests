#! /usr/bin/env nix-shell
#! nix-shell -i bash -p python39Full python39Packages.virtualenv python39Packages.pip python39Packages.pandas
#! nix-shell -I nixpkgs=./nix
# shellcheck shell=bash

set -xeuo pipefail

WORKDIR="/scratch/workdir"
rm -rf "$WORKDIR"
mkdir -p "$WORKDIR"

# create and activate python virtual env
python3 -m venv "$WORKDIR/.env_sync"
# shellcheck disable=SC1090,SC1091
. "$WORKDIR/.env_sync/bin/activate"

# install packages into python virtual env
python3 -m pip install blockfrost-python

python3 -c "import requests,pandas;"
