import pytest


def pytest_configure(config):
    keyword = config.option.keyword
    if keyword != "":
        config.option.keyword = "test_build_and_deploy or " + keyword
