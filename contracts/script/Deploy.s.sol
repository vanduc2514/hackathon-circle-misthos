// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {VmSafe} from "forge-std/Vm.sol";
import {MisthosEscrow} from "../src/MisthosEscrow.sol";

/**
 * @title Deploy
 * @notice Deploys MisthosEscrow and, when the deployment is actually broadcast,
 *         writes where it went to `deployments/<chainId>.json`, which the API
 *         reads instead of a hard-coded address.
 *
 * Deploy to Arc testnet with an encrypted keystore, and verify on the explorer
 * (Arc's explorers run Blockscout, which needs no API key):
 *
 *   cast wallet import misthos-deployer --interactive
 *   mise run contracts:deploy
 *
 * A dry run (no `--broadcast`) writes nothing: a record must only ever name a
 * contract that was sent to the chain. The API also refuses a record whose
 * address holds no code, for the case where a broadcast was sent and failed.
 *
 * Mainnet moves real USDC irreversibly, so chain 5042 is refused unless
 * MISTHOS_CONFIRM_MAINNET=5042 is set. A confirmation that names the chain is
 * harder to leave on by accident than a boolean.
 *
 * USDC is a fixed predeploy on every Arc network, so the same address works on
 * testnet and mainnet.
 */
contract Deploy is Script {
    address constant ARC_USDC = 0x3600000000000000000000000000000000000000;
    uint256 constant ARC_MAINNET = 5042;

    error MainnetNotConfirmed();

    function run() external returns (MisthosEscrow) {
        bool broadcasting = vm.isContext(VmSafe.ForgeContext.ScriptBroadcast)
            || vm.isContext(VmSafe.ForgeContext.ScriptResume);
        return deploy(
            vm.envOr("MISTHOS_ATTESTOR_ADDRESS", msg.sender),
            vm.envOr("MISTHOS_USDC_ADDRESS", ARC_USDC),
            // Where the take rate goes. Left unset, the escrow still works for issues
            // with no fee, but a release with a rate set would have nowhere to pay it.
            vm.envOr("MISTHOS_FEE_RECIPIENT_ADDRESS", address(0)),
            vm.envOr("MISTHOS_CONFIRM_MAINNET", uint256(0)),
            broadcasting ? recordPath(block.chainid) : ""
        );
    }

    /// @notice Where the record for a chain lives. Committed for Arc networks.
    function recordPath(uint256 chainId) public view returns (string memory) {
        return string.concat(vm.projectRoot(), "/deployments/", vm.toString(chainId), ".json");
    }

    /// @notice The deploy itself, separate from reading the environment and the run
    ///         mode so tests can drive it without process-wide variables and without
    ///         touching a real record.
    /// @param record Where to write the record; empty writes none.
    function deploy(
        address attestor,
        address usdc,
        address feeRecipient,
        uint256 mainnetConfirmation,
        string memory record
    ) public returns (MisthosEscrow escrow) {
        if (block.chainid == ARC_MAINNET && mainnetConfirmation != ARC_MAINNET) {
            revert MainnetNotConfirmed();
        }

        vm.startBroadcast();
        escrow = new MisthosEscrow(attestor, usdc);
        if (feeRecipient != address(0)) {
            escrow.setFeeRecipient(feeRecipient);
        }
        vm.stopBroadcast();

        console.log("MisthosEscrow deployed at", address(escrow));
        console.log("Owner                 ", escrow.owner());
        console.log("Attestor              ", attestor);
        console.log("USDC                  ", usdc);
        console.log("Fee recipient         ", feeRecipient);

        if (bytes(record).length == 0) {
            console.log("Dry run: no deployment record written");
            return escrow;
        }
        _record(escrow, attestor, usdc, feeRecipient, record);
    }

    /// @dev The record is what the API reads, so it carries everything needed to
    ///      find and trust the contract: the chain it is on, the roles it was born
    ///      with, and `deployedAtBlock`, the head when the script ran. That is at
    ///      or before the creation block, so a log scan from it misses nothing.
    function _record(
        MisthosEscrow escrow,
        address attestor,
        address usdc,
        address feeRecipient,
        string memory path
    ) internal {
        string memory key = "deployment";
        vm.serializeUint(key, "chainId", block.chainid);
        vm.serializeUint(key, "deployedAtBlock", block.number);
        vm.serializeAddress(key, "owner", escrow.owner());
        vm.serializeAddress(key, "attestor", attestor);
        vm.serializeAddress(key, "feeRecipient", feeRecipient);
        vm.serializeAddress(key, "usdc", usdc);
        string memory json = vm.serializeAddress(key, "escrow", address(escrow));

        vm.writeJson(json, path);
        console.log("Deployment record     ", path);
    }
}
