"""Tests for spending with Plutus V2 using `transaction build-raw`."""
import json
import logging
import string
from pathlib import Path
from typing import Any
from typing import List
from typing import Optional
from typing import Tuple

import allure
import hypothesis
import hypothesis.strategies as st
import pytest
from _pytest.fixtures import FixtureRequest
from cardano_clusterlib import clusterlib

from cardano_node_tests.tests import common
from cardano_node_tests.tests import plutus_common
from cardano_node_tests.utils import cluster_management
from cardano_node_tests.utils import clusterlib_utils
from cardano_node_tests.utils import configuration
from cardano_node_tests.utils import helpers
from cardano_node_tests.utils.versions import VERSIONS

LOGGER = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.skipif(
        VERSIONS.transaction_era < VERSIONS.BABBAGE,
        reason="runs only with Babbage+ TX",
    ),
    pytest.mark.skipif(
        bool(configuration.SKIP_PLUTUSV2),
        reason="needs PlutusV2 cost model",
    ),
    pytest.mark.smoke,
]

# approx. fee for Tx size
FEE_REDEEM_TXSIZE = 400_000

PLUTUS_OP_ALWAYS_SUCCEEDS = plutus_common.PlutusOp(
    script_file=plutus_common.ALWAYS_SUCCEEDS["v2"].script_file,
    datum_file=plutus_common.DATUM_42,
    redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
    execution_cost=plutus_common.ALWAYS_SUCCEEDS["v2"].execution_cost,
)

PLUTUS_OP_GUESSING_GAME_UNTYPED = plutus_common.PlutusOp(
    script_file=plutus_common.GUESSING_GAME_UNTYPED["v2"].script_file,
    datum_file=plutus_common.DATUM_42,
    redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
    execution_cost=plutus_common.GUESSING_GAME_UNTYPED["v2"].execution_cost,
)

PLUTUS_OP_ALWAYS_FAILS = plutus_common.PlutusOp(
    script_file=plutus_common.ALWAYS_FAILS["v2"].script_file,
    datum_file=plutus_common.DATUM_42,
    redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
    execution_cost=plutus_common.ALWAYS_FAILS["v2"].execution_cost,
)


@pytest.fixture
def payment_addrs(
    cluster_manager: cluster_management.ClusterManager,
    cluster: clusterlib.ClusterLib,
) -> List[clusterlib.AddressRecord]:
    """Create new payment addresses."""
    test_id = common.get_test_id(cluster)
    addrs = clusterlib_utils.create_payment_addr_records(
        *[f"{test_id}_payment_addr_{i}" for i in range(2)],
        cluster_obj=cluster,
    )

    # fund source address
    clusterlib_utils.fund_from_faucet(
        addrs[0],
        cluster_obj=cluster,
        faucet_data=cluster_manager.cache.addrs_data["user1"],
        amount=3_000_000_000,
    )

    return addrs


def _fund_script(
    temp_template: str,
    cluster: clusterlib.ClusterLib,
    payment_addr: clusterlib.AddressRecord,
    dst_addr: clusterlib.AddressRecord,
    plutus_op: plutus_common.PlutusOp,
    amount: int,
    redeem_cost: plutus_common.ScriptCost,
    use_reference_script: Optional[bool] = False,
    use_inline_datum: Optional[bool] = False,
) -> Tuple[
    List[clusterlib.UTXOData],
    List[clusterlib.UTXOData],
    List[clusterlib.UTXOData],
    clusterlib.TxRawOutput,
]:
    """Fund a Plutus script and create the locked UTxO, collateral UTxO and reference script."""
    script_address = cluster.gen_payment_addr(
        addr_name=temp_template, payment_script_file=plutus_op.script_file
    )

    # create a Tx output with a datum hash at the script address

    tx_files = clusterlib.TxFiles(
        signing_key_files=[payment_addr.skey_file],
    )

    txouts = [
        clusterlib.TxOut(
            address=script_address,
            amount=amount + redeem_cost.fee + FEE_REDEEM_TXSIZE,
            inline_datum_file=(
                plutus_op.datum_file if plutus_op.datum_file and use_inline_datum else ""
            ),
            inline_datum_value=(
                plutus_op.datum_value if plutus_op.datum_value and use_inline_datum else ""
            ),
            datum_hash_file=(
                plutus_op.datum_file if plutus_op.datum_file and not use_inline_datum else ""
            ),
            datum_hash_value=(
                plutus_op.datum_value if plutus_op.datum_value and not use_inline_datum else ""
            ),
        ),
        # for collateral
        clusterlib.TxOut(address=dst_addr.address, amount=redeem_cost.collateral),
    ]

    # for reference script
    if use_reference_script:
        txouts.append(
            clusterlib.TxOut(
                address=dst_addr.address,
                amount=amount,
                reference_script_file=plutus_op.script_file,
            )
        )

    tx_raw_output = cluster.send_tx(
        src_address=payment_addr.address,
        tx_name=f"{temp_template}_step1",
        txouts=txouts,
        tx_files=tx_files,
        # TODO: workaround for https://github.com/input-output-hk/cardano-node/issues/1892
        witness_count_add=2,
        join_txouts=False,
    )

    txid = cluster.get_txid(tx_body_file=tx_raw_output.out_file)

    script_utxos = cluster.get_utxo(txin=f"{txid}#0")
    assert script_utxos, "No script UTxO"

    collateral_utxos = cluster.get_utxo(txin=f"{txid}#1")
    assert collateral_utxos, "No collateral UTxO"

    reference_utxos = []
    if use_reference_script:
        reference_utxos = cluster.get_utxo(txin=f"{txid}#2")
        assert reference_utxos, "No reference script UTxO"

    return script_utxos, collateral_utxos, reference_utxos, tx_raw_output


@pytest.mark.testnets
class TestLockingV2:
    """Tests for Tx output locking using Plutus V2 smart contracts."""

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.parametrize("use_inline_datum", (True, False), ids=("inline_datum", "datum_file"))
    @pytest.mark.parametrize(
        "use_reference_script", (True, False), ids=("reference_script", "script_file")
    )
    def test_txout_locking(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        use_inline_datum: bool,
        use_reference_script: bool,
        request: FixtureRequest,
    ):
        """Test combinations of inline datum and datum file + reference script and script file.

        * create the necessary Tx outputs
        * check that the expected amount was locked at the script address
        * spend the locked UTxO
        * check that the expected UTxOs were correctly spent
        """
        temp_template = f"{common.get_test_id(cluster)}_{request.node.callspec.id}"
        amount = 2_000_000

        plutus_op = PLUTUS_OP_ALWAYS_SUCCEEDS

        # for mypy
        assert plutus_op.execution_cost
        assert plutus_op.datum_file
        assert plutus_op.redeemer_cbor_file

        redeem_cost = plutus_common.compute_cost(
            execution_cost=plutus_op.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        # Step 1: fund the Plutus script

        script_utxos, collateral_utxos, reference_utxos, __ = _fund_script(
            temp_template=temp_template,
            cluster=cluster,
            payment_addr=payment_addrs[0],
            dst_addr=payment_addrs[1],
            plutus_op=plutus_op,
            amount=amount,
            redeem_cost=redeem_cost,
            use_inline_datum=use_inline_datum,
            use_reference_script=use_reference_script,
        )

        plutus_txins = [
            clusterlib.ScriptTxIn(
                txins=script_utxos,
                script_file=plutus_op.script_file if not use_reference_script else "",
                reference_txin=reference_utxos[0] if use_reference_script else None,
                reference_type=clusterlib.ScriptTypes.PLUTUS_V2 if use_reference_script else "",
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_op.execution_cost.per_time,
                    plutus_op.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op.redeemer_cbor_file,
                inline_datum_present=use_inline_datum,
                datum_file=plutus_op.datum_file if not use_inline_datum else "",
            )
        ]

        tx_files_redeem = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[1].skey_file],
        )
        txouts_redeem = [
            clusterlib.TxOut(address=payment_addrs[1].address, amount=amount),
        ]

        tx_output_redeem = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txouts=txouts_redeem,
            tx_files=tx_files_redeem,
            fee=redeem_cost.fee + FEE_REDEEM_TXSIZE,
            script_txins=plutus_txins,
        )
        tx_signed_redeem = cluster.sign_tx(
            tx_body_file=tx_output_redeem.out_file,
            signing_key_files=tx_files_redeem.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        dst_init_balance = cluster.get_address_balance(payment_addrs[1].address)

        cluster.submit_tx(
            tx_file=tx_signed_redeem,
            txins=[t.txins[0] for t in tx_output_redeem.script_txins if t.txins],
        )

        assert (
            cluster.get_address_balance(payment_addrs[1].address) == dst_init_balance + amount
        ), f"Incorrect balance for destination address `{payment_addrs[1].address}`"

        script_utxos_lovelace = [u for u in script_utxos if u.coin == clusterlib.DEFAULT_COIN]
        for u in script_utxos_lovelace:
            assert not cluster.get_utxo(
                txin=f"{u.utxo_hash}#{u.utxo_ix}", coins=[clusterlib.DEFAULT_COIN]
            ), f"Inputs were NOT spent for `{u.address}`"

        if use_reference_script:
            assert cluster.get_utxo(
                txin=f"{reference_utxos[0].utxo_hash}#{reference_utxos[0].utxo_ix}",
                coins=[clusterlib.DEFAULT_COIN],
            ), "Reference input was spent"


@pytest.mark.testnets
class TestNegativeInlineDatum:
    """Tests for Tx output with inline datum that are expected to fail."""

    @allure.link(helpers.get_vcs_link())
    @hypothesis.given(datum_value=st.text())
    @common.hypothesis_settings()
    def test_lock_tx_invalid_datum(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        datum_value: str,
    ):
        """Test locking a Tx output with an invalid datum.

        Expect failure.
        """
        temp_template = common.get_test_id(cluster)
        amount = 2_000_000

        datum_file = f"{temp_template}.datum"
        with open(datum_file, "w", encoding="utf-8") as outfile:
            json.dump(f'{{"{datum_value}"}}', outfile)

        plutus_op = plutus_common.PlutusOp(
            script_file=plutus_common.ALWAYS_SUCCEEDS_PLUTUS_V2,
            datum_file=Path(datum_file),
            redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
            execution_cost=plutus_common.ALWAYS_SUCCEEDS_COST,
        )

        # for mypy
        assert plutus_op.execution_cost

        redeem_cost = plutus_common.compute_cost(
            execution_cost=plutus_op.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        # create a Tx output with an invalid inline datum at the script address

        with pytest.raises(clusterlib.CLIError) as excinfo:
            _fund_script(
                temp_template=temp_template,
                cluster=cluster,
                payment_addr=payment_addrs[0],
                dst_addr=payment_addrs[1],
                plutus_op=plutus_op,
                amount=amount,
                redeem_cost=redeem_cost,
                use_inline_datum=True,
            )
        err_str = str(excinfo.value)
        assert "JSON object expected. Unexpected value" in err_str, err_str

    @allure.link(helpers.get_vcs_link())
    def test_lock_tx_v1_script(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
    ):
        """Test locking a Tx output with an inline datum and a v1 script.

        Expect failure.
        """
        __: Any  # mypy workaround
        temp_template = common.get_test_id(cluster)
        amount = 2_000_000

        plutus_op = plutus_common.PlutusOp(
            script_file=plutus_common.ALWAYS_SUCCEEDS_PLUTUS_V1,
            datum_file=plutus_common.DATUM_42_TYPED,
            redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
            execution_cost=plutus_common.ALWAYS_SUCCEEDS_COST,
        )

        # for mypy
        assert plutus_op.execution_cost
        assert plutus_op.datum_file
        assert plutus_op.redeemer_cbor_file

        redeem_cost = plutus_common.compute_cost(
            execution_cost=plutus_op.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        script_utxos, collateral_utxos, __, __ = _fund_script(
            temp_template=temp_template,
            cluster=cluster,
            payment_addr=payment_addrs[0],
            dst_addr=payment_addrs[1],
            plutus_op=plutus_op,
            amount=amount,
            redeem_cost=redeem_cost,
            use_inline_datum=True,
        )

        plutus_txins = [
            clusterlib.ScriptTxIn(
                txins=script_utxos,
                script_file=plutus_op.script_file,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_op.execution_cost.per_time,
                    plutus_op.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op.redeemer_cbor_file,
                inline_datum_present=True,
            )
        ]

        tx_files_redeem = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[1].skey_file],
        )
        txouts_redeem = [
            clusterlib.TxOut(address=payment_addrs[1].address, amount=amount),
        ]

        tx_output_redeem = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txouts=txouts_redeem,
            tx_files=tx_files_redeem,
            fee=redeem_cost.fee + FEE_REDEEM_TXSIZE,
            script_txins=plutus_txins,
        )

        tx_signed_redeem = cluster.sign_tx(
            tx_body_file=tx_output_redeem.out_file,
            signing_key_files=tx_files_redeem.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.submit_tx(
                tx_file=tx_signed_redeem,
                txins=[t.txins[0] for t in tx_output_redeem.script_txins if t.txins],
            )
        err_str = str(excinfo.value)
        assert "InlineDatumsNotSupported" in err_str, err_str

    @allure.link(helpers.get_vcs_link())
    @hypothesis.given(datum_content=st.text(alphabet=string.ascii_letters, min_size=65))
    @common.hypothesis_settings()
    def test_lock_tx_big_datum(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        datum_content: str,
    ):
        """Test locking a Tx output with a datum bigger than the allowed size.

        Expect failure.
        """
        hypothesis.assume(datum_content)
        temp_template = common.get_test_id(cluster)
        amount = 2_000_000

        plutus_op = plutus_common.PlutusOp(
            script_file=plutus_common.ALWAYS_SUCCEEDS_PLUTUS_V2,
            datum_value=f'"{datum_content}"',
            redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
            execution_cost=plutus_common.ALWAYS_SUCCEEDS_COST,
        )

        # for mypy
        assert plutus_op.execution_cost

        redeem_cost = plutus_common.compute_cost(
            execution_cost=plutus_op.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            _fund_script(
                temp_template=temp_template,
                cluster=cluster,
                payment_addr=payment_addrs[0],
                dst_addr=payment_addrs[1],
                plutus_op=plutus_op,
                amount=amount,
                redeem_cost=redeem_cost,
                use_inline_datum=True,
            )
        err_str = str(excinfo.value)
        assert "Byte strings in script data must consist of at most 64 bytes" in err_str, err_str

    @allure.link(helpers.get_vcs_link())
    def test_lock_tx_datum_as_witness(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Test unlock a Tx output with a datum as witness.

        Expect failure.
        """
        __: Any  # mypy workaround
        temp_template = common.get_test_id(cluster)
        amount = 2_000_000

        plutus_op = PLUTUS_OP_ALWAYS_SUCCEEDS

        # for mypy
        assert plutus_op.execution_cost
        assert plutus_op.datum_file
        assert plutus_op.redeemer_cbor_file

        redeem_cost = plutus_common.compute_cost(
            execution_cost=plutus_op.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        script_utxos, collateral_utxos, __, __ = _fund_script(
            temp_template=temp_template,
            cluster=cluster,
            payment_addr=payment_addrs[0],
            dst_addr=payment_addrs[1],
            plutus_op=plutus_op,
            amount=amount,
            redeem_cost=redeem_cost,
            use_inline_datum=True,
        )

        plutus_txins = [
            clusterlib.ScriptTxIn(
                txins=script_utxos,
                script_file=plutus_op.script_file,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_op.execution_cost.per_time,
                    plutus_op.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op.redeemer_cbor_file,
                datum_file=plutus_op.datum_file,
            )
        ]

        tx_files_redeem = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[1].skey_file],
        )
        txouts_redeem = [
            clusterlib.TxOut(address=payment_addrs[1].address, amount=amount),
        ]

        tx_output_redeem = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txouts=txouts_redeem,
            tx_files=tx_files_redeem,
            fee=redeem_cost.fee + FEE_REDEEM_TXSIZE,
            script_txins=plutus_txins,
        )

        tx_signed_redeem = cluster.sign_tx(
            tx_body_file=tx_output_redeem.out_file,
            signing_key_files=tx_files_redeem.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.submit_tx(
                tx_file=tx_signed_redeem,
                txins=[t.txins[0] for t in tx_output_redeem.script_txins if t.txins],
            )
        err_str = str(excinfo.value)
        assert "NonOutputSupplimentaryDatums" in err_str, err_str


@pytest.mark.testnets
class TestReferenceScripts:
    """Tests for Tx output locking using Plutus smart contracts with reference scripts."""

    @allure.link(helpers.get_vcs_link())
    def test_reference_multiple_script(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
    ):
        """Test locking two Tx output with different V2 reference script and spending it.

        * create the Tx outputs with an inline datum at the script address
        * create the Tx outputs with the reference scripts
        * spend the locked UTxOs using the reference UTxOs
        * check that the UTxOs were correctly spent
        """
        temp_template = common.get_test_id(cluster)
        amount = 2_000_000

        plutus_op1 = PLUTUS_OP_ALWAYS_SUCCEEDS
        plutus_op2 = PLUTUS_OP_GUESSING_GAME_UNTYPED

        # for mypy
        assert plutus_op1.execution_cost and plutus_op2.execution_cost
        assert plutus_op1.datum_file and plutus_op2.datum_file
        assert plutus_op1.redeemer_cbor_file and plutus_op2.redeemer_cbor_file

        # create a Tx output with an inline datum at the script address

        script_address_1 = cluster.gen_payment_addr(
            addr_name=f"{temp_template}_addr1", payment_script_file=plutus_op1.script_file
        )

        script_address_2 = cluster.gen_payment_addr(
            addr_name=f"{temp_template}_addr2", payment_script_file=plutus_op2.script_file
        )

        redeem_cost_1 = plutus_common.compute_cost(
            execution_cost=plutus_op1.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        redeem_cost_2 = plutus_common.compute_cost(
            execution_cost=plutus_op2.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        tx_files = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[0].skey_file],
        )

        txouts = [
            clusterlib.TxOut(
                address=script_address_1,
                amount=amount + redeem_cost_1.fee,
                inline_datum_file=plutus_op1.datum_file,
            ),
            clusterlib.TxOut(
                address=script_address_2,
                amount=amount + redeem_cost_2.fee + FEE_REDEEM_TXSIZE,
                inline_datum_file=plutus_op2.datum_file,
            ),
            # for reference script
            clusterlib.TxOut(
                address=payment_addrs[1].address,
                amount=10_000_000,
                reference_script_file=plutus_op1.script_file,
            ),
            clusterlib.TxOut(
                address=payment_addrs[1].address,
                amount=10_000_000,
                reference_script_file=plutus_op2.script_file,
            ),
            # for collateral
            clusterlib.TxOut(address=payment_addrs[1].address, amount=redeem_cost_1.collateral),
            clusterlib.TxOut(address=payment_addrs[1].address, amount=redeem_cost_2.collateral),
        ]

        tx_raw_output = cluster.send_tx(
            src_address=payment_addrs[0].address,
            tx_name=f"{temp_template}_step1",
            txouts=txouts,
            tx_files=tx_files,
            # TODO: workaround for https://github.com/input-output-hk/cardano-node/issues/1892
            witness_count_add=2,
            join_txouts=False,
        )

        txid = cluster.get_txid(tx_body_file=tx_raw_output.out_file)
        script_utxos1 = cluster.get_utxo(txin=f"{txid}#0")
        script_utxos2 = cluster.get_utxo(txin=f"{txid}#1")
        reference_utxo1 = cluster.get_utxo(txin=f"{txid}#2")[0]
        reference_utxo2 = cluster.get_utxo(txin=f"{txid}#3")[0]
        collateral_utxos1 = cluster.get_utxo(txin=f"{txid}#4")
        collateral_utxos2 = cluster.get_utxo(txin=f"{txid}#5")

        #  spend the "locked" UTxO

        plutus_txins = [
            clusterlib.ScriptTxIn(
                txins=script_utxos1,
                reference_txin=reference_utxo1,
                reference_type=clusterlib.ScriptTypes.PLUTUS_V2,
                collaterals=collateral_utxos1,
                execution_units=(
                    plutus_op1.execution_cost.per_time,
                    plutus_op1.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op1.redeemer_cbor_file,
                inline_datum_present=True,
            ),
            clusterlib.ScriptTxIn(
                txins=script_utxos2,
                reference_txin=reference_utxo2,
                reference_type=clusterlib.ScriptTypes.PLUTUS_V2,
                collaterals=collateral_utxos2,
                execution_units=(
                    plutus_op2.execution_cost.per_time,
                    plutus_op2.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op2.redeemer_cbor_file,
                inline_datum_present=True,
            ),
        ]

        tx_files_redeem = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[1].skey_file],
        )
        txouts_redeem = [
            clusterlib.TxOut(address=payment_addrs[1].address, amount=amount * 2),
        ]

        tx_output_redeem = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txouts=txouts_redeem,
            tx_files=tx_files_redeem,
            fee=redeem_cost_1.fee + redeem_cost_2.fee + FEE_REDEEM_TXSIZE,
            script_txins=plutus_txins,
        )
        tx_signed_redeem = cluster.sign_tx(
            tx_body_file=tx_output_redeem.out_file,
            signing_key_files=tx_files_redeem.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        cluster.submit_tx(
            tx_file=tx_signed_redeem,
            txins=[t.txins[0] for t in tx_output_redeem.script_txins if t.txins],
        )

        # check that script address UTxO was spent
        script_utxo1 = f"{script_utxos1[0].utxo_hash}#{script_utxos1[0].utxo_ix}"
        script_utxo2 = f"{script_utxos2[0].utxo_hash}#{script_utxos2[0].utxo_ix}"
        assert (
            len(cluster.get_utxo(txin=script_utxo1)) == 0
            and len(cluster.get_utxo(txin=script_utxo2)) == 0
        ), f"Script addresses UTxO was not spent `{script_utxo1}` and `{script_utxo2}`"

    @allure.link(helpers.get_vcs_link())
    def test_reference_same_script(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
    ):
        """Test locking two Tx output with the same V2 reference script and spending it.

        * create the Tx outputs with an inline datum at the script address
        * create the Tx output with the reference script
        * spend the locked UTxOs using the reference UTxO
        * check that the UTxOs were correctly spent
        """
        temp_template = common.get_test_id(cluster)
        amount = 2_000_000

        plutus_op = PLUTUS_OP_ALWAYS_SUCCEEDS

        # for mypy
        assert plutus_op.execution_cost
        assert plutus_op.datum_file
        assert plutus_op.redeemer_cbor_file

        # create a Tx output with an inline datum at the script address

        script_address_1 = cluster.gen_payment_addr(
            addr_name=f"{temp_template}_addr1", payment_script_file=plutus_op.script_file
        )

        script_address_2 = cluster.gen_payment_addr(
            addr_name=f"{temp_template}_addr2", payment_script_file=plutus_op.script_file
        )

        redeem_cost = plutus_common.compute_cost(
            execution_cost=plutus_op.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        tx_files = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[0].skey_file],
        )

        txouts = [
            clusterlib.TxOut(
                address=script_address_1,
                amount=amount + redeem_cost.fee,
                inline_datum_file=plutus_op.datum_file,
            ),
            clusterlib.TxOut(
                address=script_address_2,
                amount=amount + redeem_cost.fee + FEE_REDEEM_TXSIZE,
                inline_datum_file=plutus_op.datum_file,
            ),
            # for reference script
            clusterlib.TxOut(
                address=payment_addrs[1].address,
                amount=10_000_000,
                reference_script_file=plutus_op.script_file,
            ),
            # for collateral
            clusterlib.TxOut(address=payment_addrs[1].address, amount=redeem_cost.collateral),
            clusterlib.TxOut(address=payment_addrs[1].address, amount=redeem_cost.collateral),
        ]

        tx_raw_output = cluster.send_tx(
            src_address=payment_addrs[0].address,
            tx_name=f"{temp_template}_step1",
            txouts=txouts,
            tx_files=tx_files,
            # TODO: workaround for https://github.com/input-output-hk/cardano-node/issues/1892
            witness_count_add=2,
            join_txouts=False,
        )

        txid = cluster.get_txid(tx_body_file=tx_raw_output.out_file)
        script_utxos1 = cluster.get_utxo(txin=f"{txid}#0")
        script_utxos2 = cluster.get_utxo(txin=f"{txid}#1")
        reference_utxo = cluster.get_utxo(txin=f"{txid}#2")[0]
        collateral_utxos1 = cluster.get_utxo(txin=f"{txid}#3")
        collateral_utxos2 = cluster.get_utxo(txin=f"{txid}#4")

        #  spend the "locked" UTxO

        plutus_txins = [
            clusterlib.ScriptTxIn(
                txins=script_utxos1,
                reference_txin=reference_utxo,
                reference_type=clusterlib.ScriptTypes.PLUTUS_V2,
                collaterals=collateral_utxos1,
                execution_units=(
                    plutus_op.execution_cost.per_time,
                    plutus_op.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op.redeemer_cbor_file,
                inline_datum_present=True,
            ),
            clusterlib.ScriptTxIn(
                txins=script_utxos2,
                reference_txin=reference_utxo,
                reference_type=clusterlib.ScriptTypes.PLUTUS_V2,
                collaterals=collateral_utxos2,
                execution_units=(
                    plutus_op.execution_cost.per_time,
                    plutus_op.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op.redeemer_cbor_file,
                inline_datum_present=True,
            ),
        ]

        tx_files_redeem = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[1].skey_file],
        )
        txouts_redeem = [
            clusterlib.TxOut(address=payment_addrs[1].address, amount=amount * 2),
        ]

        tx_output_redeem = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txouts=txouts_redeem,
            tx_files=tx_files_redeem,
            fee=redeem_cost.fee * 2 + FEE_REDEEM_TXSIZE,
            script_txins=plutus_txins,
        )
        tx_signed_redeem = cluster.sign_tx(
            tx_body_file=tx_output_redeem.out_file,
            signing_key_files=tx_files_redeem.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        cluster.submit_tx(
            tx_file=tx_signed_redeem,
            txins=[t.txins[0] for t in tx_output_redeem.script_txins if t.txins],
        )

        # check that script address UTxO was spent
        script_utxo1 = f"{script_utxos1[0].utxo_hash}#{script_utxos1[0].utxo_ix}"
        script_utxo2 = f"{script_utxos2[0].utxo_hash}#{script_utxos2[0].utxo_ix}"
        assert (
            len(cluster.get_utxo(txin=script_utxo1)) == 0
            and len(cluster.get_utxo(txin=script_utxo2)) == 0
        ), f"Script addresses UTxO was not spent `{script_utxo1}` and `{script_utxo2}`"

    @allure.link(helpers.get_vcs_link())
    def test_mix_reference_attached_script(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
    ):
        """Test locking a Tx output with an attached V2 script and one using reference V2 script.

        * create the Tx output with an attached script
        * create the Tx output with the reference script
        * spend the locked UTxOs
        * check that the UTxOs were correctly spent
        """
        temp_template = common.get_test_id(cluster)
        amount = 2_000_000

        plutus_op1 = PLUTUS_OP_ALWAYS_SUCCEEDS
        plutus_op2 = PLUTUS_OP_GUESSING_GAME_UNTYPED

        # for mypy
        assert plutus_op1.execution_cost and plutus_op2.execution_cost
        assert plutus_op1.datum_file and plutus_op2.datum_file
        assert plutus_op1.redeemer_cbor_file and plutus_op2.redeemer_cbor_file

        # create a Tx output with an inline datum at the script address

        script_address_1 = cluster.gen_payment_addr(
            addr_name=f"{temp_template}_addr1", payment_script_file=plutus_op1.script_file
        )

        script_address_2 = cluster.gen_payment_addr(
            addr_name=f"{temp_template}_addr2", payment_script_file=plutus_op2.script_file
        )

        redeem_cost_1 = plutus_common.compute_cost(
            execution_cost=plutus_op1.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        redeem_cost_2 = plutus_common.compute_cost(
            execution_cost=plutus_op2.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        tx_files = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[0].skey_file],
        )

        txouts = [
            clusterlib.TxOut(
                address=script_address_1,
                amount=amount + redeem_cost_1.fee,
                datum_hash_file=plutus_op1.datum_file,
            ),
            clusterlib.TxOut(
                address=script_address_2,
                amount=amount + redeem_cost_2.fee + FEE_REDEEM_TXSIZE,
                inline_datum_file=plutus_op2.datum_file,
            ),
            # for reference script
            clusterlib.TxOut(
                address=payment_addrs[1].address,
                amount=10_000_000,
                reference_script_file=plutus_op2.script_file,
            ),
            # for collateral
            clusterlib.TxOut(address=payment_addrs[1].address, amount=redeem_cost_1.collateral),
            clusterlib.TxOut(address=payment_addrs[1].address, amount=redeem_cost_2.collateral),
        ]

        tx_raw_output = cluster.send_tx(
            src_address=payment_addrs[0].address,
            tx_name=f"{temp_template}_step1",
            txouts=txouts,
            tx_files=tx_files,
            # TODO: workaround for https://github.com/input-output-hk/cardano-node/issues/1892
            witness_count_add=2,
            join_txouts=False,
        )

        txid = cluster.get_txid(tx_body_file=tx_raw_output.out_file)
        script_utxos1 = cluster.get_utxo(txin=f"{txid}#0")
        script_utxos2 = cluster.get_utxo(txin=f"{txid}#1")
        reference_utxo = cluster.get_utxo(txin=f"{txid}#2")[0]
        collateral_utxos1 = cluster.get_utxo(txin=f"{txid}#3")
        collateral_utxos2 = cluster.get_utxo(txin=f"{txid}#4")

        #  spend the "locked" UTxO

        plutus_txins = [
            clusterlib.ScriptTxIn(
                txins=script_utxos1,
                script_file=plutus_op1.script_file,
                collaterals=collateral_utxos1,
                execution_units=(
                    plutus_op1.execution_cost.per_time,
                    plutus_op1.execution_cost.per_space,
                ),
                datum_file=plutus_op1.datum_file,
                redeemer_cbor_file=plutus_op1.redeemer_cbor_file,
            ),
            clusterlib.ScriptTxIn(
                txins=script_utxos2,
                reference_txin=reference_utxo,
                reference_type=clusterlib.ScriptTypes.PLUTUS_V2,
                collaterals=collateral_utxos2,
                execution_units=(
                    plutus_op2.execution_cost.per_time,
                    plutus_op2.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op2.redeemer_cbor_file,
                inline_datum_present=True,
            ),
        ]

        tx_files_redeem = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[1].skey_file],
        )
        txouts_redeem = [
            clusterlib.TxOut(address=payment_addrs[1].address, amount=amount * 2),
        ]

        tx_output_redeem = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txouts=txouts_redeem,
            tx_files=tx_files_redeem,
            fee=redeem_cost_1.fee + redeem_cost_2.fee + FEE_REDEEM_TXSIZE,
            script_txins=plutus_txins,
        )
        tx_signed_redeem = cluster.sign_tx(
            tx_body_file=tx_output_redeem.out_file,
            signing_key_files=tx_files_redeem.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        cluster.submit_tx(
            tx_file=tx_signed_redeem,
            txins=[t.txins[0] for t in tx_output_redeem.script_txins if t.txins],
        )

        # check that script address UTxO was spent
        script_utxo1 = f"{script_utxos1[0].utxo_hash}#{script_utxos1[0].utxo_ix}"
        script_utxo2 = f"{script_utxos2[0].utxo_hash}#{script_utxos2[0].utxo_ix}"
        assert (
            len(cluster.get_utxo(txin=script_utxo1)) == 0
            and len(cluster.get_utxo(txin=script_utxo2)) == 0
        ), f"Script addresses UTxO was not spent `{script_utxo1}` and `{script_utxo2}`"

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.parametrize("script_type", ("simple", "plutus_v1", "plutus_v2"))
    def test_spend_reference_script(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        script_type: str,
    ):
        """Test spend a UTxO that holds a reference script.

        * create the Tx output with the reference script
        * check that the expected amount was transferred
        * spend the UTxO
        * check that the UTxO was spent
        """
        temp_template = f"{common.get_test_id(cluster)}_{script_type}"
        amount = 2_000_000

        if script_type.startswith("plutus"):
            plutus_version = script_type.split("_")[-1]
            script_file = plutus_common.ALWAYS_SUCCEEDS[plutus_version].script_file
        else:
            keyhash = cluster.get_payment_vkey_hash(payment_addrs[0].vkey_file)
            script_content = {"keyHash": keyhash, "type": "sig"}
            script_file = Path(f"{temp_template}.script")
            with open(script_file, "w", encoding="utf-8") as out_json:
                json.dump(script_content, out_json)

        # create a Tx output with the reference script

        tx_files_step1 = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[0].skey_file],
        )

        txouts_step1 = [
            clusterlib.TxOut(
                address=payment_addrs[1].address,
                amount=amount,
                reference_script_file=script_file,
            )
        ]

        tx_raw_output_step1 = cluster.send_tx(
            src_address=payment_addrs[0].address,
            tx_name=f"{temp_template}_step1",
            txouts=txouts_step1,
            tx_files=tx_files_step1,
        )

        txid = cluster.get_txid(tx_body_file=tx_raw_output_step1.out_file)
        reference_txin = f"{txid}#0"
        reference_utxo = cluster.get_utxo(txin=reference_txin)

        assert reference_utxo[0].amount == amount, "Incorrect amount transferred"

        # spend the Tx output with the reference script
        src_addr = payment_addrs[1]
        dst_addr = payment_addrs[0]

        txouts_step2 = [clusterlib.TxOut(address=dst_addr.address, amount=-1)]
        tx_files_step2 = clusterlib.TxFiles(signing_key_files=[src_addr.skey_file])

        cluster.send_tx(
            src_address=payment_addrs[1].address,
            tx_name=f"{temp_template}_step1",
            txins=reference_utxo,
            txouts=txouts_step2,
            tx_files=tx_files_step2,
        )

        # check that reference script UTxO was spent
        assert not cluster.get_utxo(
            txin=reference_txin
        ), f"Reference script UTxO was not spent '{reference_txin}`"


@pytest.mark.testnets
class TestNegativeReferenceScripts:
    """Tests for Tx output with reference scripts that are expected to fail."""

    @allure.link(helpers.get_vcs_link())
    def test_not_a_script(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
    ):
        """Test locking a Tx output with an invalid reference script.

        Expect failure.
        """
        temp_template = common.get_test_id(cluster)
        amount = 2_000_000

        plutus_op = plutus_common.PlutusOp(
            script_file=plutus_common.DATUM_42_TYPED,
            datum_file=plutus_common.DATUM_42_TYPED,
            redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
            execution_cost=plutus_common.ALWAYS_SUCCEEDS_V2_COST,
        )

        assert plutus_op.execution_cost

        redeem_cost = plutus_common.compute_cost(
            execution_cost=plutus_op.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        # create a Tx output with an inline datum at the script address

        with pytest.raises(clusterlib.CLIError) as excinfo:
            _fund_script(
                temp_template=temp_template,
                cluster=cluster,
                payment_addr=payment_addrs[0],
                dst_addr=payment_addrs[1],
                plutus_op=plutus_op,
                amount=amount,
                redeem_cost=redeem_cost,
            )
        err_str = str(excinfo.value)
        assert "Syntax error in script" in err_str, err_str

    @allure.link(helpers.get_vcs_link())
    def test_two_scripts_one_fail(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
    ):
        """Test locking two Tx with different Plutus reference scripts in single Tx, one fails.

        Expect failure.
        """
        temp_template = common.get_test_id(cluster)
        amount = 2_000_000

        plutus_op1 = PLUTUS_OP_ALWAYS_SUCCEEDS
        plutus_op2 = PLUTUS_OP_ALWAYS_FAILS

        # for mypy
        assert plutus_op1.execution_cost and plutus_op2.execution_cost
        assert plutus_op1.datum_file and plutus_op2.datum_file
        assert plutus_op1.redeemer_cbor_file and plutus_op2.redeemer_cbor_file

        # create a Tx output with an inline datum at the script address

        script_address_1 = cluster.gen_payment_addr(
            addr_name=f"{temp_template}_addr1", payment_script_file=plutus_op1.script_file
        )

        script_address_2 = cluster.gen_payment_addr(
            addr_name=f"{temp_template}_addr2", payment_script_file=plutus_op2.script_file
        )

        redeem_cost_1 = plutus_common.compute_cost(
            execution_cost=plutus_op1.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        redeem_cost_2 = plutus_common.compute_cost(
            execution_cost=plutus_op2.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        tx_files = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[0].skey_file],
        )

        txouts = [
            clusterlib.TxOut(
                address=script_address_1,
                amount=amount + redeem_cost_1.fee,
                inline_datum_file=plutus_op1.datum_file,
            ),
            clusterlib.TxOut(
                address=script_address_2,
                amount=amount + redeem_cost_2.fee + FEE_REDEEM_TXSIZE,
                inline_datum_file=plutus_op2.datum_file,
            ),
            # for reference script
            clusterlib.TxOut(
                address=payment_addrs[1].address,
                amount=10_000_000,
                reference_script_file=plutus_op1.script_file,
            ),
            clusterlib.TxOut(
                address=payment_addrs[1].address,
                amount=10_000_000,
                reference_script_file=plutus_op2.script_file,
            ),
            # for collateral
            clusterlib.TxOut(address=payment_addrs[1].address, amount=redeem_cost_1.collateral),
            clusterlib.TxOut(address=payment_addrs[1].address, amount=redeem_cost_2.collateral),
        ]

        tx_raw_output = cluster.send_tx(
            src_address=payment_addrs[0].address,
            tx_name=f"{temp_template}_step1",
            txouts=txouts,
            tx_files=tx_files,
            # TODO: workaround for https://github.com/input-output-hk/cardano-node/issues/1892
            witness_count_add=2,
            join_txouts=False,
        )

        txid = cluster.get_txid(tx_body_file=tx_raw_output.out_file)
        script_utxos1 = cluster.get_utxo(txin=f"{txid}#0")
        script_utxos2 = cluster.get_utxo(txin=f"{txid}#1")
        reference_utxo1 = cluster.get_utxo(txin=f"{txid}#2")[0]
        reference_utxo2 = cluster.get_utxo(txin=f"{txid}#3")[0]
        collateral_utxos1 = cluster.get_utxo(txin=f"{txid}#4")
        collateral_utxos2 = cluster.get_utxo(txin=f"{txid}#5")

        #  spend the "locked" UTxO

        plutus_txins = [
            clusterlib.ScriptTxIn(
                txins=script_utxos1,
                reference_txin=reference_utxo1,
                reference_type=clusterlib.ScriptTypes.PLUTUS_V2,
                collaterals=collateral_utxos1,
                execution_units=(
                    plutus_op1.execution_cost.per_time,
                    plutus_op1.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op1.redeemer_cbor_file,
                inline_datum_present=True,
            ),
            clusterlib.ScriptTxIn(
                txins=script_utxos2,
                reference_txin=reference_utxo2,
                reference_type=clusterlib.ScriptTypes.PLUTUS_V2,
                collaterals=collateral_utxos2,
                execution_units=(
                    plutus_op2.execution_cost.per_time,
                    plutus_op2.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op2.redeemer_cbor_file,
                inline_datum_present=True,
            ),
        ]

        tx_files_redeem = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[1].skey_file],
        )
        txouts_redeem = [
            clusterlib.TxOut(address=payment_addrs[1].address, amount=amount * 2),
        ]

        tx_output_redeem = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txouts=txouts_redeem,
            tx_files=tx_files_redeem,
            fee=redeem_cost_1.fee + redeem_cost_2.fee + FEE_REDEEM_TXSIZE,
            script_txins=plutus_txins,
        )
        tx_signed_redeem = cluster.sign_tx(
            tx_body_file=tx_output_redeem.out_file,
            signing_key_files=tx_files_redeem.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.submit_tx(
                tx_file=tx_signed_redeem,
                txins=[t.txins[0] for t in tx_output_redeem.script_txins if t.txins],
            )
        err_str = str(excinfo.value)
        script2_hash = helpers.decode_bech32(bech32=script_address_2)[2:]
        assert rf"ScriptHash \"{script2_hash}\") fails" in str(excinfo.value) in err_str, err_str

    @allure.link(helpers.get_vcs_link())
    def test_lock_tx_v1_reference_script(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
    ):
        """Test locking a Tx output with a Plutus V1 reference script.

        Expect failure.
        """
        temp_template = common.get_test_id(cluster)
        amount = 2_000_000

        plutus_op = plutus_common.PlutusOp(
            script_file=plutus_common.ALWAYS_SUCCEEDS["v1"].script_file,
            datum_file=plutus_common.DATUM_42_TYPED,
            redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
            execution_cost=plutus_common.ALWAYS_SUCCEEDS["v1"].execution_cost,
        )

        # for mypy
        assert plutus_op.execution_cost
        assert plutus_op.datum_file
        assert plutus_op.redeemer_cbor_file

        redeem_cost = plutus_common.compute_cost(
            execution_cost=plutus_op.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        # Step 1: fund the Plutus script

        script_utxos, collateral_utxos, reference_utxos, __ = _fund_script(
            temp_template=temp_template,
            cluster=cluster,
            payment_addr=payment_addrs[0],
            dst_addr=payment_addrs[1],
            plutus_op=plutus_op,
            amount=amount,
            redeem_cost=redeem_cost,
            use_reference_script=True,
            use_inline_datum=True,
        )

        plutus_txins = [
            clusterlib.ScriptTxIn(
                txins=script_utxos,
                reference_txin=reference_utxos[0],
                reference_type=clusterlib.ScriptTypes.PLUTUS_V2,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_op.execution_cost.per_time,
                    plutus_op.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op.redeemer_cbor_file,
                inline_datum_present=True,
            )
        ]

        tx_files_redeem = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[1].skey_file],
        )
        txouts_redeem = [
            clusterlib.TxOut(address=payment_addrs[1].address, amount=amount),
        ]

        tx_output_redeem = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txouts=txouts_redeem,
            tx_files=tx_files_redeem,
            fee=redeem_cost.fee + FEE_REDEEM_TXSIZE,
            script_txins=plutus_txins,
        )
        tx_signed_redeem = cluster.sign_tx(
            tx_body_file=tx_output_redeem.out_file,
            signing_key_files=tx_files_redeem.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        # create a Tx output with an inline datum at the script address

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.submit_tx(
                tx_file=tx_signed_redeem,
                txins=[t.txins[0] for t in tx_output_redeem.script_txins if t.txins],
            )
        err_str = str(excinfo.value)
        assert "ReferenceInputsNotSupported" in err_str, err_str

    @allure.link(helpers.get_vcs_link())
    def test_v1_attached_v2_reference(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
    ):
        """Test locking a Tx output with an attached V1 script and one using reference V2 script.

        Expect failure.
        """
        temp_template = common.get_test_id(cluster)
        amount = 2_000_000

        plutus_op1 = plutus_common.PlutusOp(
            script_file=plutus_common.ALWAYS_SUCCEEDS["v1"].script_file,
            datum_file=plutus_common.DATUM_42_TYPED,
            redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
            execution_cost=plutus_common.ALWAYS_SUCCEEDS["v1"].execution_cost,
        )

        plutus_op2 = PLUTUS_OP_GUESSING_GAME_UNTYPED

        # for mypy
        assert plutus_op1.execution_cost and plutus_op2.execution_cost
        assert plutus_op1.datum_file and plutus_op2.datum_file
        assert plutus_op1.redeemer_cbor_file and plutus_op2.redeemer_cbor_file

        # create a Tx output with an inline datum at the script address

        script_address_1 = cluster.gen_payment_addr(
            addr_name=f"{temp_template}_addr1", payment_script_file=plutus_op1.script_file
        )

        script_address_2 = cluster.gen_payment_addr(
            addr_name=f"{temp_template}_addr2", payment_script_file=plutus_op2.script_file
        )

        redeem_cost_1 = plutus_common.compute_cost(
            execution_cost=plutus_op1.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        redeem_cost_2 = plutus_common.compute_cost(
            execution_cost=plutus_op2.execution_cost, protocol_params=cluster.get_protocol_params()
        )

        tx_files = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[0].skey_file],
        )

        txouts = [
            clusterlib.TxOut(
                address=script_address_1,
                amount=amount + redeem_cost_1.fee,
                datum_hash_file=plutus_op1.datum_file,
            ),
            clusterlib.TxOut(
                address=script_address_2,
                amount=amount + redeem_cost_2.fee + FEE_REDEEM_TXSIZE,
                inline_datum_file=plutus_op2.datum_file,
            ),
            # for reference script
            clusterlib.TxOut(
                address=payment_addrs[1].address,
                amount=10_000_000,
                reference_script_file=plutus_op2.script_file,
            ),
            # for collateral
            clusterlib.TxOut(address=payment_addrs[1].address, amount=redeem_cost_1.collateral),
            clusterlib.TxOut(address=payment_addrs[1].address, amount=redeem_cost_2.collateral),
        ]

        tx_raw_output = cluster.send_tx(
            src_address=payment_addrs[0].address,
            tx_name=f"{temp_template}_step1",
            txouts=txouts,
            tx_files=tx_files,
            # TODO: workaround for https://github.com/input-output-hk/cardano-node/issues/1892
            witness_count_add=2,
            join_txouts=False,
        )

        txid = cluster.get_txid(tx_body_file=tx_raw_output.out_file)
        script_utxos1 = cluster.get_utxo(txin=f"{txid}#0")
        script_utxos2 = cluster.get_utxo(txin=f"{txid}#1")
        reference_utxo = cluster.get_utxo(txin=f"{txid}#2")[0]
        collateral_utxos1 = cluster.get_utxo(txin=f"{txid}#3")
        collateral_utxos2 = cluster.get_utxo(txin=f"{txid}#4")

        #  spend the "locked" UTxO

        plutus_txins = [
            clusterlib.ScriptTxIn(
                txins=script_utxos1,
                script_file=plutus_op1.script_file,
                collaterals=collateral_utxos1,
                execution_units=(
                    plutus_op1.execution_cost.per_time,
                    plutus_op1.execution_cost.per_space,
                ),
                datum_file=plutus_op1.datum_file,
                redeemer_cbor_file=plutus_op1.redeemer_cbor_file,
            ),
            clusterlib.ScriptTxIn(
                txins=script_utxos2,
                reference_txin=reference_utxo,
                reference_type=clusterlib.ScriptTypes.PLUTUS_V2,
                collaterals=collateral_utxos2,
                execution_units=(
                    plutus_op2.execution_cost.per_time,
                    plutus_op2.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_op2.redeemer_cbor_file,
                inline_datum_present=True,
            ),
        ]

        tx_files_redeem = clusterlib.TxFiles(
            signing_key_files=[payment_addrs[1].skey_file],
        )
        txouts_redeem = [
            clusterlib.TxOut(address=payment_addrs[1].address, amount=amount * 2),
        ]

        tx_output_redeem = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txouts=txouts_redeem,
            tx_files=tx_files_redeem,
            fee=redeem_cost_1.fee + redeem_cost_2.fee + FEE_REDEEM_TXSIZE,
            script_txins=plutus_txins,
        )
        tx_signed_redeem = cluster.sign_tx(
            tx_body_file=tx_output_redeem.out_file,
            signing_key_files=tx_files_redeem.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.submit_tx(
                tx_file=tx_signed_redeem,
                txins=[t.txins[0] for t in tx_output_redeem.script_txins if t.txins],
            )
        err_str = str(excinfo.value)
        assert "ReferenceInputsNotSupported" in err_str, err_str
