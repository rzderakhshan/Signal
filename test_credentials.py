import os
import sys
import subprocess
import pytest

def run_scanner(args, env):
    # Run the scanner in a subprocess with the given environment
    # Return exit code and output
    cmd = [sys.executable, "scanner.py"] + args
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr

def test_dry_run_no_credentials():
    # Dry run should pass (exit code 0 or 1 for no data, but not 2 for config)
    env = os.environ.copy()
    if "TELEGRAM_BOT_TOKEN" in env: del env["TELEGRAM_BOT_TOKEN"]
    if "TELEGRAM_CHAT_ID" in env: del env["TELEGRAM_CHAT_ID"]
    if "SIGNAL_CHAT_ID" in env: del env["SIGNAL_CHAT_ID"]
    
    code, out, err = run_scanner(["--github-dry-run"], env)
    assert code != 2
    assert "Configure TELEGRAM_BOT_TOKEN" not in err

def test_production_no_credentials_fails():
    env = os.environ.copy()
    if "TELEGRAM_BOT_TOKEN" in env: del env["TELEGRAM_BOT_TOKEN"]
    if "TELEGRAM_CHAT_ID" in env: del env["TELEGRAM_CHAT_ID"]
    if "SIGNAL_CHAT_ID" in env: del env["SIGNAL_CHAT_ID"]
    
    code, out, err = run_scanner([], env)
    assert code == 2
    assert "Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID" in err

def test_production_with_credentials_starts():
    env = os.environ.copy()
    env["TELEGRAM_BOT_TOKEN"] = "mock_token"
    env["TELEGRAM_CHAT_ID"] = "mock_id"
    
    code, out, err = run_scanner([], env)
    # Should not fail due to missing credentials. Might fail due to no data (exit code 1)
    assert code != 2
    assert "Configure TELEGRAM_BOT_TOKEN" not in err
