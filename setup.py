from setuptools import setup, find_namespace_packages

with open("cli_anything/alexa/README.md") as f:
    long_description = f.read()

setup(
    name="cli-anything-alexa",
    version="0.1.0",
    description="CLI harness for Amazon Alexa — appliance/group/routine/notification management over the unofficial web API",
    long_description=long_description,
    long_description_content_type="text/markdown",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    install_requires=[
        "click>=8.0.0",
        "prompt-toolkit>=3.0.0",
        "alexapy>=1.27.0",
    ],
    extras_require={
        # Unit tests only need pytest — the pure logic (applianceId parsing,
        # whitelist filtering, table formatting) is tested without alexapy
        # or a live account.
        "test": [
            "pytest>=7.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "cli-anything-alexa=cli_anything.alexa.alexa_cli:main",
        ],
    },
    package_data={
        "cli_anything.alexa": ["skills/*.md", "README.md"],
    },
    include_package_data=True,
    python_requires=">=3.10",
)
