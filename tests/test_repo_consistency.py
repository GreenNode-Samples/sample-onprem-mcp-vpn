"""Guards against drift between the code and its documentation / example files."""

import asyncio
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (REPO / path).read_text()


def test_env_example_lists_exactly_the_variables_the_code_reads():
    code = text("src/onprem_mcp/main.py") + text("src/onprem_mcp/healthcheck.py")
    read = set(re.findall(r'environ(?:\.get)?[\[(]"([A-Z_]+)"', code)) | set(re.findall(r'_env_flag\("([A-Z_]+)"\)', code))
    listed = set(re.findall(r"^#?\s*([A-Z][A-Z_]+)=", text(".env.example"), re.MULTILINE))
    assert read == listed


def test_docs_name_every_tool_and_every_policy_action(m):
    names = [tool.name for tool in asyncio.run(m.mcp.list_tools())]
    assert len(names) == 6
    for name in names:
        assert f"`erp__{name}`" in text("README.md"), name
        assert f"`erp__{name}`" in text("infra/greennode/README.md"), name


def test_compose_trusts_only_the_fixed_caddy_address():
    compose = text("infra/onprem/docker-compose.yml")
    address = re.search(r"ipv4_address: (\S+)", compose).group(1)
    assert f'TRUSTED_PROXIES: "{address}"' in compose


def test_health_probe_is_defined_once_in_the_image():
    assert "healthcheck:" not in text("infra/onprem/docker-compose.yml")
    assert 'CMD ["python", "healthcheck.py"]' in text("Dockerfile")


def test_caddy_image_is_pinned_identically_in_compose_and_lab():
    compose = re.search(r"image: (caddy:\S+)", text("infra/onprem/docker-compose.yml")).group(1)
    lab = re.search(r'CADDY_IMAGE="(caddy:\S+?)"', text("lab/lab_setup_onprem.sh")).group(1)
    assert compose == lab and re.fullmatch(r"caddy:\d+\.\d+\.\d+", compose)


def test_nftables_never_flushes_the_whole_ruleset():
    rules = text("infra/onprem/firewall/nftables.conf")
    assert not re.search(r"^\s*flush\s+ruleset", rules, re.MULTILINE)
    assert "table inet onprem_mcp" in rules and "delete table inet onprem_mcp" in rules
