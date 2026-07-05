from importlib.metadata import version

import pytest
from packaging.version import Version


@pytest.mark.parametrize(
    ("package", "minimum", "maximum"),
    [
        ("langchain-core", "1.4.8", "2.0.0"),
        ("langchain-openai", "1.3.3", "2.0.0"),
        ("langgraph", "1.2.7", "2.0.0"),
        ("langgraph-checkpoint", "4.1.1", "5.0.0"),
        ("langgraph-checkpoint-sqlite", "3.1.0", "4.0.0"),
    ],
)
def test_supported_langchain_stack_is_installed(
    package: str,
    minimum: str,
    maximum: str,
) -> None:
    installed = Version(version(package))

    assert installed >= Version(minimum)
    assert installed < Version(maximum)
