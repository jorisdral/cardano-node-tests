import logging
import random
from pathlib import Path
from typing import List

import allure
import pytest
from _pytest.tmpdir import TempdirFactory

from cardano_node_tests.utils import clusterlib
from cardano_node_tests.utils import clusterlib_utils
from cardano_node_tests.utils import helpers
from cardano_node_tests.utils import parallel_run

LOGGER = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def temp_dir(tmp_path_factory: TempdirFactory):
    """Create a temporary dir and change to it."""
    tmp_path = Path(tmp_path_factory.mktemp(helpers.get_id_for_mktemp(__file__)))
    with helpers.change_cwd(tmp_path):
        yield tmp_path


# use the "temp_dir" fixture for all tests automatically
pytestmark = pytest.mark.usefixtures("temp_dir")


def multisig_tx(
    cluster_obj: clusterlib.ClusterLib,
    temp_template: str,
    src_address: str,
    dst_address: str,
    amount: int,
    multisig_script: Path,
    payment_skey_files: List[Path],
    script_is_src=False,
):
    # record initial balances
    src_init_balance = cluster_obj.get_address_balance(src_address)
    dst_init_balance = cluster_obj.get_address_balance(dst_address)

    # create TX body
    destinations = [clusterlib.TxOut(address=dst_address, amount=amount)]
    witness_count_add = len(payment_skey_files)
    if script_is_src:
        # TODO: workaround for https://github.com/input-output-hk/cardano-node/issues/1892
        witness_count_add += 5

    ttl = cluster_obj.calculate_tx_ttl()
    fee = cluster_obj.calculate_tx_fee(
        src_address=src_address,
        tx_name=temp_template,
        txouts=destinations,
        ttl=ttl,
        witness_count_add=witness_count_add,
    )
    tx_raw_output = cluster_obj.build_raw_tx(
        src_address=src_address,
        tx_name=temp_template,
        txouts=destinations,
        fee=fee,
        ttl=ttl,
    )

    # create witness file for each key
    witness_files = [
        cluster_obj.witness_tx(
            tx_body_file=tx_raw_output.out_file,
            tx_name=f"{temp_template}_skey{idx}",
            signing_key_files=[skey],
        )
        for idx, skey in enumerate(payment_skey_files)
    ]
    if script_is_src:
        witness_files.append(
            cluster_obj.witness_tx(
                tx_body_file=tx_raw_output.out_file,
                tx_name=f"{temp_template}_script",
                script_file=multisig_script,
            )
        )

    # sign TX using witness files
    tx_witnessed_file = cluster_obj.assemble_tx(
        tx_body_file=tx_raw_output.out_file,
        witness_files=witness_files,
        tx_name=temp_template,
    )

    # submit signed TX
    cluster_obj.submit_tx(tx_witnessed_file)
    cluster_obj.wait_for_new_block(new_blocks=2)

    # check final balances
    assert (
        cluster_obj.get_address_balance(src_address)
        == src_init_balance - tx_raw_output.fee - amount
    ), f"Incorrect balance for source address `{src_address}`"

    assert (
        cluster_obj.get_address_balance(dst_address) == dst_init_balance + amount
    ), f"Incorrect balance for script address `{dst_address}`"


class TestBasic:
    @pytest.fixture
    def payment_addrs(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster: clusterlib.ClusterLib,
    ) -> List[clusterlib.AddressRecord]:
        """Create new payment addresses."""
        data_key = id(TestBasic)
        cached_value = cluster_manager.cache.test_data.get(data_key)
        if cached_value:
            return cached_value  # type: ignore

        addrs = clusterlib_utils.create_payment_addr_records(
            *[f"multi_addr{i}" for i in range(20)], cluster_obj=cluster
        )
        cluster_manager.cache.test_data[data_key] = addrs

        # fund source addresses
        clusterlib_utils.fund_from_faucet(
            addrs[0],
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=20_000_000,
        )

        return addrs

    @allure.link(helpers.get_vcs_link())
    def test_script_addr_length(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Check that script address length is the same as lenght of other addresses.

        There was an issue that script address was 32 bytes instead of 28 bytes.
        """
        temp_template = helpers.get_func_name()

        payment_vkey_files = [p.vkey_file for p in payment_addrs]

        # create multisig script
        multisig_script = cluster.build_multisig_script(
            script_name=temp_template,
            script_type_arg=clusterlib.MultiSigTypeArgs.ALL,
            payment_vkey_files=payment_vkey_files,
        )

        # create script address
        script_addr = cluster.gen_script_addr(multisig_script)

        # check script address length
        assert len(script_addr) == len(payment_addrs[0].address)

    @allure.link(helpers.get_vcs_link())
    def test_multisig_all(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Send funds to and from script address using the "all" script."""
        temp_template = helpers.get_func_name()

        payment_vkey_files = [p.vkey_file for p in payment_addrs]
        payment_skey_files = [p.skey_file for p in payment_addrs]

        # create multisig script
        multisig_script = cluster.build_multisig_script(
            script_name=temp_template,
            script_type_arg=clusterlib.MultiSigTypeArgs.ALL,
            payment_vkey_files=payment_vkey_files,
        )

        # create script address
        script_addr = cluster.gen_script_addr(multisig_script)

        # send funds to script address
        multisig_tx(
            cluster_obj=cluster,
            temp_template=f"{temp_template}_to",
            src_address=payment_addrs[0].address,
            dst_address=script_addr,
            amount=2_000_000,
            multisig_script=multisig_script,
            payment_skey_files=[payment_skey_files[0]],
        )

        # send funds from script address
        multisig_tx(
            cluster_obj=cluster,
            temp_template=f"{temp_template}_from",
            src_address=script_addr,
            dst_address=payment_addrs[0].address,
            amount=1000,
            multisig_script=multisig_script,
            payment_skey_files=payment_skey_files,
            script_is_src=True,
        )

    @allure.link(helpers.get_vcs_link())
    def test_multisig_any(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Send funds to and from script address using the "any" script."""
        temp_template = helpers.get_func_name()

        payment_vkey_files = [p.vkey_file for p in payment_addrs]
        payment_skey_files = [p.skey_file for p in payment_addrs]

        skeys_len = len(payment_skey_files)

        # create multisig script
        multisig_script = cluster.build_multisig_script(
            script_name=temp_template,
            script_type_arg=clusterlib.MultiSigTypeArgs.ANY,
            payment_vkey_files=payment_vkey_files,
        )

        # create script address
        script_addr = cluster.gen_script_addr(multisig_script)

        # send funds to script address
        multisig_tx(
            cluster_obj=cluster,
            temp_template=f"{temp_template}_to",
            src_address=payment_addrs[0].address,
            dst_address=script_addr,
            amount=5_000_000,
            multisig_script=multisig_script,
            payment_skey_files=[payment_skey_files[0]],
        )

        # send funds from script address using single witness
        for i in range(5):
            multisig_tx(
                cluster_obj=cluster,
                temp_template=f"{temp_template}_from_single_{i}",
                src_address=script_addr,
                dst_address=payment_addrs[0].address,
                amount=1000,
                multisig_script=multisig_script,
                payment_skey_files=[payment_skey_files[random.randrange(0, skeys_len)]],
                script_is_src=True,
            )

        # send funds from script address using multiple witnesses
        for i in range(5):
            num_of_skeys = random.randrange(2, skeys_len)
            multisig_tx(
                cluster_obj=cluster,
                temp_template=f"{temp_template}_from_multi_{i}",
                src_address=script_addr,
                dst_address=payment_addrs[0].address,
                amount=1000,
                multisig_script=multisig_script,
                payment_skey_files=random.sample(payment_skey_files, k=num_of_skeys),
                script_is_src=True,
            )

    @allure.link(helpers.get_vcs_link())
    def test_multisig_atleast(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Send funds to and from script address using the "atLeast" script."""
        temp_template = helpers.get_func_name()

        payment_vkey_files = [p.vkey_file for p in payment_addrs]
        payment_skey_files = [p.skey_file for p in payment_addrs]

        skeys_len = len(payment_skey_files)
        required = skeys_len - 4

        # create multisig script
        multisig_script = cluster.build_multisig_script(
            script_name=temp_template,
            script_type_arg=clusterlib.MultiSigTypeArgs.AT_LEAST,
            payment_vkey_files=payment_vkey_files,
            required=required,
        )

        # create script address
        script_addr = cluster.gen_script_addr(multisig_script)

        # send funds to script address
        num_of_skeys = random.randrange(required, skeys_len)
        multisig_tx(
            cluster_obj=cluster,
            temp_template=f"{temp_template}_to",
            src_address=payment_addrs[0].address,
            dst_address=script_addr,
            amount=2_000_000,
            multisig_script=multisig_script,
            payment_skey_files=[payment_skey_files[0]],
        )

        # send funds from script address
        for i in range(5):
            num_of_skeys = random.randrange(required, skeys_len)
            multisig_tx(
                cluster_obj=cluster,
                temp_template=f"{temp_template}_from_{i}",
                src_address=script_addr,
                dst_address=payment_addrs[0].address,
                amount=1000,
                multisig_script=multisig_script,
                payment_skey_files=random.sample(payment_skey_files, k=num_of_skeys),
                script_is_src=True,
            )

    @allure.link(helpers.get_vcs_link())
    def test_normal_tx_to_script_addr(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Send funds to script address using TX signed with skeys (not using witness files)."""
        temp_template = helpers.get_func_name()
        src_address = payment_addrs[0].address
        amount = 1000

        # create multisig script
        multisig_script = cluster.build_multisig_script(
            script_name=temp_template,
            script_type_arg=clusterlib.MultiSigTypeArgs.ALL,
            payment_vkey_files=[p.vkey_file for p in payment_addrs],
        )

        # create script address
        script_addr = cluster.gen_script_addr(multisig_script)

        # record initial balances
        src_init_balance = cluster.get_address_balance(src_address)
        dst_init_balance = cluster.get_address_balance(script_addr)

        # send funds to script address
        destinations = [clusterlib.TxOut(address=script_addr, amount=amount)]
        tx_files = clusterlib.TxFiles(signing_key_files=[payment_addrs[0].skey_file])

        tx_raw_output = cluster.send_funds(
            src_address=src_address,
            tx_name=temp_template,
            destinations=destinations,
            tx_files=tx_files,
        )
        cluster.wait_for_new_block(new_blocks=2)

        # check final balances
        assert (
            cluster.get_address_balance(src_address)
            == src_init_balance - tx_raw_output.fee - amount
        ), f"Incorrect balance for source address `{src_address}`"

        assert (
            cluster.get_address_balance(script_addr) == dst_init_balance + amount
        ), f"Incorrect balance for destination address `{script_addr}`"


class TestNegative:
    @pytest.fixture
    def payment_addrs(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster: clusterlib.ClusterLib,
    ) -> List[clusterlib.AddressRecord]:
        """Create new payment addresses."""
        data_key = id(TestNegative)
        cached_value = cluster_manager.cache.test_data.get(data_key)
        if cached_value:
            return cached_value  # type: ignore

        addrs = clusterlib_utils.create_payment_addr_records(
            *[f"multi_neg_addr{i}" for i in range(10)], cluster_obj=cluster
        )
        cluster_manager.cache.test_data[data_key] = addrs

        # fund source addresses
        clusterlib_utils.fund_from_faucet(
            addrs[0],
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
        )

        return addrs

    @allure.link(helpers.get_vcs_link())
    def test_multisig_all_missing_skey(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Send funds from script address using the "all" script, omit one skey."""
        temp_template = helpers.get_func_name()

        payment_vkey_files = [p.vkey_file for p in payment_addrs]
        payment_skey_files = [p.skey_file for p in payment_addrs]

        # create multisig script
        multisig_script = cluster.build_multisig_script(
            script_name=temp_template,
            script_type_arg=clusterlib.MultiSigTypeArgs.ALL,
            payment_vkey_files=payment_vkey_files,
        )

        # create script address
        script_addr = cluster.gen_script_addr(multisig_script)

        # send funds to script address
        multisig_tx(
            cluster_obj=cluster,
            temp_template=temp_template,
            src_address=payment_addrs[0].address,
            dst_address=script_addr,
            amount=300_000,
            multisig_script=multisig_script,
            payment_skey_files=[payment_skey_files[0]],
        )

        # send funds from script address, omit one skey
        with pytest.raises(clusterlib.CLIError) as excinfo:
            multisig_tx(
                cluster_obj=cluster,
                temp_template=temp_template,
                src_address=script_addr,
                dst_address=payment_addrs[0].address,
                amount=1000,
                multisig_script=multisig_script,
                payment_skey_files=payment_skey_files[:-1],
                script_is_src=True,
            )
        assert "ScriptWitnessNotValidatingUTXOW" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_multisig_any_unlisted_skey(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Send funds from script address using the "any" script with unlisted skey."""
        temp_template = helpers.get_func_name()

        payment_vkey_files = [p.vkey_file for p in payment_addrs[:-1]]
        payment_skey_files = [p.skey_file for p in payment_addrs]

        # create multisig script
        multisig_script = cluster.build_multisig_script(
            script_name=temp_template,
            script_type_arg=clusterlib.MultiSigTypeArgs.ANY,
            payment_vkey_files=payment_vkey_files,
        )

        # create script address
        script_addr = cluster.gen_script_addr(multisig_script)

        # send funds to script address
        multisig_tx(
            cluster_obj=cluster,
            temp_template=temp_template,
            src_address=payment_addrs[0].address,
            dst_address=script_addr,
            amount=300_000,
            multisig_script=multisig_script,
            payment_skey_files=[payment_skey_files[0]],
        )

        # send funds from script address, use skey that is not listed in the script
        with pytest.raises(clusterlib.CLIError) as excinfo:
            multisig_tx(
                cluster_obj=cluster,
                temp_template=temp_template,
                src_address=script_addr,
                dst_address=payment_addrs[0].address,
                amount=1000,
                multisig_script=multisig_script,
                payment_skey_files=[payment_skey_files[-1]],
                script_is_src=True,
            )
        assert "ScriptWitnessNotValidatingUTXOW" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_multisig_atleast_low_num_of_skeys(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Send funds from script address using the "atLeast" script. Num of skeys < required."""
        temp_template = helpers.get_func_name()

        payment_vkey_files = [p.vkey_file for p in payment_addrs]
        payment_skey_files = [p.skey_file for p in payment_addrs]

        skeys_len = len(payment_skey_files)
        required = skeys_len - 4

        # create multisig script
        multisig_script = cluster.build_multisig_script(
            script_name=temp_template,
            script_type_arg=clusterlib.MultiSigTypeArgs.AT_LEAST,
            payment_vkey_files=payment_vkey_files,
            required=required,
        )

        # create script address
        script_addr = cluster.gen_script_addr(multisig_script)

        # send funds to script address
        num_of_skeys = random.randrange(required, skeys_len)
        multisig_tx(
            cluster_obj=cluster,
            temp_template=temp_template,
            src_address=payment_addrs[0].address,
            dst_address=script_addr,
            amount=300_000,
            multisig_script=multisig_script,
            payment_skey_files=[payment_skey_files[0]],
        )

        # send funds from script address, use lower number of skeys then required
        for num_of_skeys in range(required):
            with pytest.raises(clusterlib.CLIError) as excinfo:
                multisig_tx(
                    cluster_obj=cluster,
                    temp_template=temp_template,
                    src_address=script_addr,
                    dst_address=payment_addrs[0].address,
                    amount=1000,
                    multisig_script=multisig_script,
                    payment_skey_files=random.sample(payment_skey_files, k=num_of_skeys),
                    script_is_src=True,
                )
            assert "ScriptWitnessNotValidatingUTXOW" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_normal_tx_from_script_addr(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Send funds from script address using TX signed with skeys (not using witness files)."""
        temp_template = helpers.get_func_name()

        payment_vkey_files = [p.vkey_file for p in payment_addrs]
        payment_skey_files = [p.skey_file for p in payment_addrs]

        # create multisig script
        multisig_script = cluster.build_multisig_script(
            script_name=temp_template,
            script_type_arg=clusterlib.MultiSigTypeArgs.ANY,
            payment_vkey_files=payment_vkey_files,
        )

        # create script address
        script_addr = cluster.gen_script_addr(multisig_script)

        # send funds to script address
        multisig_tx(
            cluster_obj=cluster,
            temp_template=temp_template,
            src_address=payment_addrs[0].address,
            dst_address=script_addr,
            amount=300_000,
            multisig_script=multisig_script,
            payment_skey_files=[payment_skey_files[0]],
        )

        # send funds from script address
        destinations_from = [clusterlib.TxOut(address=payment_addrs[0].address, amount=1000)]
        tx_files_from = clusterlib.TxFiles(signing_key_files=[payment_addrs[0].skey_file])

        # cannot send the TX without signing it using witness files
        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.send_funds(
                src_address=script_addr,
                tx_name=temp_template,
                destinations=destinations_from,
                tx_files=tx_files_from,
            )
        assert "MissingScriptWitnessesUTXOW" in str(excinfo.value)