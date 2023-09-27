from contextlib import nullcontext

import pytest
import yaml

from image_management import parse_image_config, update_images


@pytest.mark.parametrize(
    "image_config_str, expected_images_dict, context_raised",
    [
        (
            # Test a basic case
            yaml.dump({"key1": "value1", "key2": "value2"}),
            {"key1": "value1", "key2": "value2"},
            nullcontext(),
        ),
        (
            # Test that we remove empty values from the dict
            yaml.dump({"key1": "value1", "key2": ""}),
            {"key1": "value1"},
            nullcontext(),
        ),
        (
            # Test empty string, which is converted to empty dict
            "",
            {},
            nullcontext(),
        ),
        (
            # Test invalid yaml
            "{",
            {},
            pytest.raises(yaml.YAMLError),
        ),
    ],
)
def test_parse_image_config(image_config_str, expected_images_dict, context_raised):
    # Arrange

    # Act
    with context_raised:
        actual_images = parse_image_config(image_config_str)

        # Assert
        assert actual_images == expected_images_dict


@pytest.mark.parametrize(
    "default_images, custom_images, expected_images",
    [
        (
            {"key1": "value1", "key2": "value2"},
            {"key2": "value2b", "key3": "value3"},
            {"key1": "value1", "key2": "value2b", "key3": "value3"},
        )
    ],
)
def test_update_images(default_images, custom_images, expected_images):
    actual_images = update_images(default_images, custom_images)

    assert actual_images == expected_images
