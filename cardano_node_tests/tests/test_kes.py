"""Tests for KES period."""
import json
import logging
import os
import shutil
import time
from pathlib import Path

import allure
import pytest
from _pytest.tmpdir import TempdirFactory

from cardano_node_tests.utils import clusterlib
from cardano_node_tests.utils import clusterlib_utils
from cardano_node_tests.utils import devops_cluster
from cardano_node_tests.utils import helpers
from cardano_node_tests.utils import logfiles
from cardano_node_tests.utils import parallel_run

LOGGER = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def create_temp_dir(tmp_path_factory: TempdirFactory):
    """Create a temporary dir."""
    return Path(tmp_path_factory.mktemp(helpers.get_id_for_mktemp(__file__))).resolve()


@pytest.fixture
def temp_dir(create_temp_dir: Path):
    """Change to a temporary dir."""
    with helpers.change_cwd(create_temp_dir):
        yield create_temp_dir


@pytest.fixture
def cluster_lock_pool2(cluster_manager: parallel_run.ClusterManager) -> clusterlib.ClusterLib:
    return cluster_manager.get(lock_resources=["node-pool2"])


@pytest.fixture(scope="module")
def short_kes_start_cluster(tmp_path_factory: TempdirFactory) -> Path:
    """Update *slotsPerKESPeriod* and *maxKESEvolutions*."""
    pytest_globaltemp = helpers.get_pytest_globaltemp(tmp_path_factory)

    # need to lock because this same fixture can run on several workers in parallel
    with helpers.FileLockIfXdist(f"{pytest_globaltemp}/startup_files_short_kes.lock"):
        destdir = pytest_globaltemp / "startup_files_short_kes"
        destdir.mkdir(exist_ok=True)

        # return existing script if it is already generated by other worker
        if (destdir / "start-cluster").exists():
            return destdir / "start-cluster"

        startup_files = devops_cluster.copy_startup_files(destdir=destdir)
        with open(startup_files.genesis_spec) as fp_in:
            genesis_spec = json.load(fp_in)

        genesis_spec["slotsPerKESPeriod"] = 300
        genesis_spec["maxKESEvolutions"] = 5

        with open(startup_files.genesis_spec, "wt") as fp_out:
            json.dump(genesis_spec, fp_out)

        return startup_files.start_script


@pytest.fixture
def cluster_kes(
    cluster_manager: parallel_run.ClusterManager, short_kes_start_cluster: Path
) -> clusterlib.ClusterLib:
    return cluster_manager.get(singleton=True, cleanup=True, start_cmd=str(short_kes_start_cluster))


# use the "temp_dir" fixture for all tests automatically
pytestmark = pytest.mark.usefixtures("temp_dir")


@pytest.mark.run(order=3)
class TestKES:
    """Basic tests for KES period."""

    @allure.link(helpers.get_vcs_link())
    def test_expired_kes(
        self,
        cluster_kes: clusterlib.ClusterLib,
    ):
        """Test expired KES."""
        cluster = cluster_kes

        expire_timeout = int(
            cluster.slots_per_kes_period * cluster.slot_length * cluster.max_kes_evolutions + 1
        )

        expected_errors = [
            ("*.stdout", "Could not obtain ledger view for slot"),
            ("*.stdout", "KESKeyAlreadyPoisoned"),
            ("*.stdout", "KESCouldNotEvolve"),
        ]
        with logfiles.expect_errors(expected_errors):
            LOGGER.info(f"Waiting for {expire_timeout} sec for KES expiration.")
            time.sleep(expire_timeout)

            init_slot = cluster.get_last_block_slot_no()

            kes_period_timeout = int(cluster.slots_per_kes_period * cluster.slot_length + 1)
            LOGGER.info(f"Waiting for {kes_period_timeout} sec for next KES period.")
            time.sleep(kes_period_timeout)

        assert cluster.get_last_block_slot_no() == init_slot, "Unexpected new slots"

    @allure.link(helpers.get_vcs_link())
    def test_opcert_past_kes_period(
        self,
        cluster_lock_pool2: clusterlib.ClusterLib,
        cluster_manager: parallel_run.ClusterManager,
    ):
        """Start a stake pool with an operational certificate created with expired `--kes-period`.

        * generate new operational certificate with `--kes-period` in the past
        * restart the node with the new operational certificate
        * check that the pool is not producing any blocks
        * restore the original operational certificate and restart the node
        """
        pool_name = "node-pool2"
        node_name = "pool2"
        cluster = cluster_lock_pool2

        temp_template = helpers.get_func_name()
        pool_rec = cluster_manager.cache.addrs_data[pool_name]

        node_cold = pool_rec["cold_key_pair"]
        stake_pool_id = cluster.get_stake_pool_id(node_cold.vkey_file)
        stake_pool_id_dec = helpers.decode_bech32(stake_pool_id)

        opcert_file: Path = pool_rec["pool_operational_cert"]

        def _wait_epoch_chores(this_epoch: int):
            # wait for next epoch
            if cluster.get_last_block_epoch() == this_epoch:
                cluster.wait_for_new_epoch()

            # wait for the end of the epoch
            time.sleep(clusterlib_utils.time_to_next_epoch_start(cluster) - 5)

            # save ledger state
            clusterlib_utils.save_ledger_state(
                cluster_obj=cluster,
                name_template=f"{temp_template}_{cluster.get_last_block_epoch()}",
            )

        with cluster_manager.restart_on_failure():
            # generate new operational certificate with `--kes-period` in the past
            invalid_opcert_file = cluster.gen_node_operational_cert(
                node_name=node_name,
                node_kes_vkey_file=pool_rec["kes_key_pair"].vkey_file,
                node_cold_skey_file=pool_rec["cold_key_pair"].skey_file,
                node_cold_counter_file=pool_rec["cold_key_pair"].counter_file,
                kes_period=cluster.get_last_block_kes_period() - 1,
            )

            expected_errors = [
                (f"{node_name}.stdout", "TPraosCannotForgeKeyNotUsableYet"),
            ]
            with logfiles.expect_errors(expected_errors):
                # restart the node with the new operational certificate
                shutil.copy(invalid_opcert_file, opcert_file)
                devops_cluster.restart_node(node_name)

                LOGGER.info("Checking blocks production for 5 epochs.")
                this_epoch = -1
                for __ in range(5):
                    _wait_epoch_chores(this_epoch)
                    this_epoch = cluster.get_last_block_epoch()

                    # check that the pool is not producing any blocks
                    blocks_made = cluster.get_ledger_state()["nesBcur"]["unBlocksMade"]
                    if blocks_made:
                        assert (
                            stake_pool_id_dec not in blocks_made
                        ), f"The pool '{pool_name}' has produced blocks in epoch {this_epoch}"

            # generate new operational certificate with valid `--kes-period`
            os.remove(opcert_file)
            valid_opcert_file = cluster.gen_node_operational_cert(
                node_name=node_name,
                node_kes_vkey_file=pool_rec["kes_key_pair"].vkey_file,
                node_cold_skey_file=pool_rec["cold_key_pair"].skey_file,
                node_cold_counter_file=pool_rec["cold_key_pair"].counter_file,
                kes_period=cluster.get_last_block_kes_period(),
            )
            # copy the new certificate and restart the node
            shutil.move(str(valid_opcert_file), str(opcert_file))
            devops_cluster.restart_node(node_name)

            LOGGER.info("Checking blocks production for another 3 epochs.")
            for __ in range(5):
                _wait_epoch_chores(this_epoch)
                this_epoch = cluster.get_last_block_epoch()

                # check that the pool is not producing any blocks
                blocks_made = cluster.get_ledger_state()["nesBcur"]["unBlocksMade"]
                assert (
                    stake_pool_id_dec in blocks_made
                ), f"The pool '{pool_name}' has not produced blocks in epoch {this_epoch}"

    @allure.link(helpers.get_vcs_link())
    def test_update_valid_opcert(
        self,
        cluster_lock_pool2: clusterlib.ClusterLib,
        cluster_manager: parallel_run.ClusterManager,
    ):
        """Update a valid operational certificate with another valid operational certificate.

        * generate new operational certificate with valid `--kes-period`
        * restart the node with the new operational certificate
        * check that the pool is still producing blocks
        """
        pool_name = "node-pool2"
        node_name = "pool2"
        cluster = cluster_lock_pool2

        temp_template = helpers.get_func_name()
        pool_rec = cluster_manager.cache.addrs_data[pool_name]

        node_cold = pool_rec["cold_key_pair"]
        stake_pool_id = cluster.get_stake_pool_id(node_cold.vkey_file)
        stake_pool_id_dec = helpers.decode_bech32(stake_pool_id)

        opcert_file = pool_rec["pool_operational_cert"]

        with cluster_manager.restart_on_failure():
            # generate new operational certificate with valid `--kes-period`
            new_opcert_file = cluster.gen_node_operational_cert(
                node_name=node_name,
                node_kes_vkey_file=pool_rec["kes_key_pair"].vkey_file,
                node_cold_skey_file=pool_rec["cold_key_pair"].skey_file,
                node_cold_counter_file=pool_rec["cold_key_pair"].counter_file,
                kes_period=cluster.get_last_block_kes_period(),
            )

            # restart the node with the new operational certificate
            shutil.copy(new_opcert_file, opcert_file)
            devops_cluster.restart_node(node_name)

            LOGGER.info("Checking blocks production for 5 epochs.")
            this_epoch = -1
            for __ in range(5):
                # wait for next epoch
                if cluster.get_last_block_epoch() == this_epoch:
                    cluster.wait_for_new_epoch()

                # wait for the end of the epoch
                time.sleep(clusterlib_utils.time_to_next_epoch_start(cluster) - 5)
                this_epoch = cluster.get_last_block_epoch()

                # save ledger state
                clusterlib_utils.save_ledger_state(
                    cluster_obj=cluster,
                    name_template=f"{temp_template}_{this_epoch}",
                )

                # check that the pool is still producing blocks
                blocks_made = cluster.get_ledger_state()["nesBcur"]["unBlocksMade"]
                if blocks_made:
                    assert (
                        stake_pool_id_dec in blocks_made
                    ), f"The pool '{pool_name}' has not produced blocks in epoch {this_epoch}"

    @allure.link(helpers.get_vcs_link())
    def test_no_kes_period_arg(
        self,
        cluster: clusterlib.ClusterLib,
        cluster_manager: parallel_run.ClusterManager,
        temp_dir: Path,
    ):
        """Try to generate new operational certificate without specifying the `--kes-period`.

        Expect failure.
        """
        pool_name = "node-pool2"
        pool_rec = cluster_manager.cache.addrs_data[pool_name]

        temp_template = helpers.get_func_name()
        out_file = temp_dir / f"{temp_template}_shouldnt_exist.opcert"

        # try to generate new operational certificate without specifying the `--kes-period`
        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.cli(
                [
                    "node",
                    "issue-op-cert",
                    "--kes-verification-key-file",
                    str(pool_rec["kes_key_pair"].vkey_file),
                    "--cold-signing-key-file",
                    str(pool_rec["cold_key_pair"].skey_file),
                    "--operational-certificate-issue-counter",
                    str(pool_rec["cold_key_pair"].counter_file),
                    "--out-file",
                    str(out_file),
                ]
            )
        assert "Missing: --kes-period NATURAL" in str(excinfo.value)
        assert not out_file.exists(), "New operational certificate was generated"
