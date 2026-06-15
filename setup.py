from setuptools import setup, find_namespace_packages

with open("cli_anything/alexa/README.md") as f:
    long_description = f.read()

setup(
    name="cli-anything-alexa",
    version="0.2.0",
    description="CLI harness for Amazon Alexa — appliance/group/routine/notification management over the unofficial web API, with a browser-proxy login (no Home Assistant required)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Jon Williams",
    url="https://github.com/jonwilliams84/cli-anything-alexa",
    license="MIT",
    keywords=["alexa", "amazon", "echo", "smart-home", "cli", "alexapy", "home-assistant"],
    packages=find_namespace_packages(include=["cli_anything.*"]),
    install_requires=[
        "click>=8.0.0",
        "prompt-toolkit>=3.0.0",
        # 1.27.0 first shipped AlexaProxy; the browser-proxy login is the
        # primary auth path, so require a version that has it.
        "alexapy>=1.27.0",
    ],
    extras_require={
        # Unit tests only need pytest — the pure logic (applianceId parsing,
        # whitelist filtering, table formatting, proxy URL formatting) is
        # tested without alexapy or a live account.
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
    # Fresh proxy/scripted logins are persisted by alexapy on YOUR Python and
    # load back fine on 3.10+. Python 3.14 is needed ONLY to import a pickle
    # written on Python 3.14 (e.g. Home Assistant's) — the cookie
    # ``partitioned`` attribute is unpicklable on older interpreters.
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Topic :: Home Automation",
        "Topic :: Utilities",
    ],
)
