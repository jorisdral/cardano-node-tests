#! /usr/bin/env nix-shell
#! nix-shell -i bash -p python39Full python39Packages.virtualenv python39Packages.pip python39Packages.pandas
#! nix-shell -I nixpkgs=./nix
# shellcheck shell=bash

set -xeuo pipefail

echo " ==== set WORKDIR"
WORKDIR="/scratch/workdir"
rm -rf "$WORKDIR"
mkdir -p "$WORKDIR"

echo " ==== create and activate python virtual env"
python3 -m venv "$WORKDIR/.env_sync"
# shellcheck disable=SC1090,SC1091
. "$WORKDIR/.env_sync/bin/activate"

# shellcheck disable=SC2046
echo "test: $(python -c 'import sys, sys.prefix == sys.base_prefix')"

echo " ==== install packages into python virtual env"
python3 -m pip install blockfrost-python

python3 -c "import requests,pandas;"

tag_no1=$1
tag_no2=$2
hydra_eval_no1=$3
hydra_eval_no2=$4

echo " ==== start sync test"
python ./sync_tests/node_sync_test.py -t1 "$tag_no1" -t2 "$tag_no2" -e "mainnet" -e1 "$hydra_eval_no1" -e2 "$hydra_eval_no2"

echo " ==== write sync test values into the db"
python ./sync_tests/node_write_sync_values_to_db.py -e "mainnet"
