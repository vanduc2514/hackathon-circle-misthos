// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Deploy} from "../script/Deploy.s.sol";
import {MisthosEscrow} from "../src/MisthosEscrow.sol";
import {MockUsdcToken} from "./support/MockUsdcToken.sol";

contract DeployTest is Test {
    Deploy script;

    address constant ATTESTOR = address(0xA77E5);
    address constant USDC = address(0x5DC);
    address constant FEES = address(0xFEE5);

    function setUp() public {
        script = new Deploy();
        vm.createDir(string.concat(vm.projectRoot(), "/deployments/.test"), true);
    }

    /// @dev Tests write only under deployments/.test/, which is gitignored, so a
    ///      test can never overwrite, delete or fake a committed Arc record.
    function _scratch(string memory name) internal view returns (string memory) {
        return string.concat(vm.projectRoot(), "/deployments/.test/", name, ".json");
    }

    // The API trusts the record, not a constant: what it reads back must be the
    // contract that was actually created, with the roles it was actually given.
    function test_the_record_names_the_deployed_contract_and_its_roles() public {
        string memory path = _scratch("roles");
        MisthosEscrow escrow = script.deploy(ATTESTOR, USDC, FEES, 0, path);

        string memory json = vm.readFile(path);
        assertEq(vm.parseJsonAddress(json, ".escrow"), address(escrow));
        assertEq(vm.parseJsonAddress(json, ".attestor"), ATTESTOR);
        assertEq(vm.parseJsonAddress(json, ".usdc"), USDC);
        assertEq(vm.parseJsonAddress(json, ".feeRecipient"), FEES);
        assertEq(escrow.feeRecipient(), FEES);
        assertEq(vm.parseJsonAddress(json, ".owner"), escrow.owner());
        assertEq(vm.parseJsonUint(json, ".chainId"), block.chainid);

        vm.removeFile(path);
    }

    // A dry run sends nothing, so it must record nothing: a record naming an
    // address that was never deployed would point the API at an empty account.
    function test_a_dry_run_writes_no_record() public {
        string memory path = _scratch("dry-run");
        string memory before = _recordOrNothing();
        script.deploy(ATTESTOR, USDC, FEES, 0, "");
        assertFalse(vm.exists(path));
        assertEq(_recordOrNothing(), before);
    }

    // Outside a broadcast, `run()` itself must take the dry-run path.
    function test_run_outside_a_broadcast_leaves_the_real_record_alone() public {
        vm.setEnv("MISTHOS_USDC_ADDRESS", vm.toString(USDC));
        vm.setEnv("MISTHOS_FEE_RECIPIENT_ADDRESS", vm.toString(FEES));
        string memory before = _recordOrNothing();
        MisthosEscrow escrow = script.run();
        assertEq(_recordOrNothing(), before);
        assertEq(escrow.feeRecipient(), FEES);
    }

    // The platform sets a take rate on every issue, eight percent at the least, and
    // the escrow refuses a release with a rate and nowhere to pay it. An escrow
    // deployed without a recipient would take money it could never release (#127).
    function test_a_deploy_without_a_fee_recipient_is_refused() public {
        vm.expectRevert(Deploy.FeeRecipientRequired.selector);
        script.deploy(ATTESTOR, USDC, address(0), 0, "");
    }

    // The escrow the script deploys runs the platform's own sequence end to end: the
    // tier's rate and the approval, the publisher's commitment, then a release that
    // pays the contributor and the treasury from the same commitment.
    function test_an_escrow_the_script_deploys_pays_a_release_at_a_tier_rate() public {
        MockUsdcToken usdc = new MockUsdcToken();
        MisthosEscrow escrow = script.deploy(ATTESTOR, address(usdc), FEES, 0, "");
        bytes32 issue = keccak256("ISS-1009");
        address publisher = address(0xB0B);
        uint64 deadline = uint64(block.timestamp + 14 days);

        vm.startPrank(escrow.owner());
        escrow.setFee(issue, 1200); // the Open tier's rate
        escrow.setCeiling(issue, 500e6, publisher, deadline);
        vm.stopPrank();

        usdc.mint(publisher, 500e6);
        vm.startPrank(publisher);
        usdc.approve(address(escrow), 500e6);
        escrow.commit(issue, 500e6, deadline);
        vm.stopPrank();

        vm.prank(ATTESTOR);
        escrow.release(issue, address(0xC0DE), 500e6);
        assertEq(usdc.balanceOf(FEES), 60e6);
        assertEq(usdc.balanceOf(address(0xC0DE)), 440e6);
    }

    /// @dev The real record for this chain as it stands, or "" when there is none.
    ///      Compared before and after rather than required absent: a developer who
    ///      deployed to a local chain has a real record there, and it must survive.
    function _recordOrNothing() internal view returns (string memory) {
        string memory path = script.recordPath(block.chainid);
        return vm.exists(path) ? vm.readFile(path) : "";
    }

    // Mainnet moves real USDC irreversibly; a deploy there must be a decision,
    // never the side effect of pointing the script at the wrong RPC.
    function test_mainnet_is_refused_without_a_confirmation_naming_it() public {
        vm.chainId(5042);
        vm.expectRevert(Deploy.MainnetNotConfirmed.selector);
        script.deploy(ATTESTOR, USDC, FEES, 0, "");
    }

    // The confirmation is the chain id, not a flag, so it cannot be left on
    // from a testnet session and silently apply to mainnet.
    function test_mainnet_deploys_once_the_chain_is_named() public {
        vm.chainId(5042);
        MisthosEscrow escrow = script.deploy(ATTESTOR, USDC, FEES, 5042, "");
        assertEq(escrow.attestor(), ATTESTOR);
    }
}
