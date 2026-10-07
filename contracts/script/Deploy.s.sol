// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {MisthosEscrow} from "../src/MisthosEscrow.sol";

/**
 * Deploy to Arc testnet:
 *
 *   forge script script/Deploy.s.sol --rpc-url $ARC_TESTNET_RPC_URL --broadcast
 *
 * Never pass a private key as a CLI flag outside local testing. Use an encrypted
 * keystore instead:
 *
 *   cast wallet import misthos-deployer --interactive
 *   forge script script/Deploy.s.sol --rpc-url $ARC_TESTNET_RPC_URL \
 *     --account misthos-deployer --broadcast
 *
 * USDC is a fixed predeploy on every Arc network, so the same address works on
 * testnet and mainnet.
 */
contract Deploy is Script {
    address constant ARC_USDC = 0x3600000000000000000000000000000000000000;

    function run() external returns (MisthosEscrow escrow) {
        address attestor = vm.envOr("MISTHOS_ATTESTOR_ADDRESS", msg.sender);
        address usdc = vm.envOr("MISTHOS_USDC_ADDRESS", ARC_USDC);
        // Where the take rate goes. Left unset, the escrow still works for issues
        // with no fee, but a release with a rate set would have nowhere to pay it.
        address feeRecipient = vm.envOr("MISTHOS_FEE_RECIPIENT_ADDRESS", address(0));

        vm.startBroadcast();
        escrow = new MisthosEscrow(attestor, usdc);
        if (feeRecipient != address(0)) {
            escrow.setFeeRecipient(feeRecipient);
        }
        vm.stopBroadcast();

        console.log("MisthosEscrow deployed at", address(escrow));
        console.log("Attestor              ", attestor);
        console.log("USDC                  ", usdc);
        console.log("Fee recipient         ", feeRecipient);
    }
}
