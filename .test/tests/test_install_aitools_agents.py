"""Tests for the version-gated `aitools --agents` mapping in install.sh and install.ps1.

`gemini` and `kiro` are only valid `databricks aitools install --agents` values on
newer CLIs, and an unknown agent fails the whole install. The installers only pass
them when the installed CLI is new enough. Neither installer can be sourced (both
run on load), so the mapping functions are extracted and run against a stub CLI.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"
SYSTEM_PATH = "/usr/bin:/bin"
PWSH = shutil.which("pwsh")  # resolved here: the tests run pwsh with a stub-only PATH
TOOLS = "cursor gemini windsurf kiro"

# (installed CLI version or None for "not installed", expected --agents value)
CASES = [
    (None, "cursor"),
    ("1.11.9", "cursor"),
    ("1.12.0", "cursor,gemini"),
    ("1.19.9", "cursor,gemini"),
    ("1.20.0", "cursor,gemini,kiro"),
    ("1.100.0", "cursor,gemini,kiro"),
]


def _bash_functions() -> str:
    src = INSTALL_SH.read_text()
    consts = re.findall(r"(?m)^MIN_AITOOLS_(?:GEMINI|KIRO)_CLI_VERSION=.*$", src)
    fns = []
    for name in ("version_gte", "databricks_cli_version", "map_aitools_agents"):
        match = re.search(rf"(?ms)^{name}\(\) \{{.*?^\}}$", src)
        assert match, f"{name} not found in install.sh"
        fns.append(match.group(0))
    assert len(consts) == 2, "CLI floor constants not found in install.sh"
    return "\n".join(consts + fns)


def _stub_cli(tmp_path: Path, version: str | None) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if version is not None:
        stub = bin_dir / "databricks"
        stub.write_text(f'#!/bin/sh\necho "Databricks CLI v{version}"\n')
        stub.chmod(0o755)
    return bin_dir


@pytest.fixture(autouse=True)
def _no_system_cli():
    if shutil.which("databricks", path=SYSTEM_PATH):
        pytest.skip("a databricks CLI on the system PATH would shadow the stub")


def _run_bash(tmp_path: Path, version: str | None, tools: str) -> subprocess.CompletedProcess:
    fns = tmp_path / "fns.sh"
    fns.write_text(_bash_functions())
    script = f'set -e\n. "{fns}"\nTOOLS="{tools}"\nmap_aitools_agents\necho "$AITOOLS_AGENTS"\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{_stub_cli(tmp_path, version)}:{SYSTEM_PATH}"},
    )


@pytest.mark.parametrize("version,expected", CASES)
def test_install_sh_maps_agents_by_cli_version(tmp_path, version, expected):
    result = _run_bash(tmp_path, version, TOOLS)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_install_sh_skipped_last_agent_does_not_trip_set_e(tmp_path):
    # kiro is the last tool and below its floor: the mapping must still return 0 under `set -e`
    result = _run_bash(tmp_path, "1.19.9", "cursor kiro")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cursor"


PS_HARNESS = r"""
param([string]$Path, [string]$Version, [string]$Tools)
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw "install.ps1 has parse errors" }
$names = @('Get-DatabricksCliVersion', 'Resolve-AitoolsAgents')
$fns = $ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in $names }, $true)
$consts = $ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left.Extent.Text -match '^\$MinAitools(Gemini|Kiro)CliVersion$' }, $false)
if ($fns.Count -ne 2 -or $consts.Count -ne 2) { throw "mapping functions or CLI floors not found" }
foreach ($node in @($consts) + @($fns)) { . ([scriptblock]::Create($node.Extent.Text)) }
if ($Version) { function databricks { "Databricks CLI v$Version" } }
$script:Tools = $Tools
Resolve-AitoolsAgents
$script:AitoolsAgents
"""


@pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
@pytest.mark.parametrize("version,expected", CASES)
def test_install_ps1_maps_agents_by_cli_version(tmp_path, version, expected):
    harness = tmp_path / "harness.ps1"
    harness.write_text(PS_HARNESS)
    result = subprocess.run(
        [
            str(PWSH),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            "-Path",
            str(INSTALL_PS1),
            "-Version",
            version or "",
            "-Tools",
            TOOLS,
        ],
        capture_output=True,
        text=True,
        env={"PATH": f"{_stub_cli(tmp_path, None)}:{SYSTEM_PATH}", "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
