"""AWS Offerings Catalog.

This module loads the service catalog file and can be used to query
instance types and pricing information for AWS.
"""
import typing
from typing import Dict, List, Optional, Tuple

from sky import config
from sky.clouds.service_catalog import common

if typing.TYPE_CHECKING:
    from sky.clouds import cloud
    import pandas as pd

_UPDATE_FREQUENCY_HOURS = 7
_auto_update_frequency_hours = _UPDATE_FREQUENCY_HOURS
if not config.sky_config.catalog.aws.auto_update:
    _auto_update_frequency_hours = None


# Filter the dataframe to only include the preferred regions.
def area_filter_fn(df: 'pd.DataFrame') -> 'pd.DataFrame':
    preferred_areas = config.sky_config.catalog.aws.preferred_area
    if preferred_areas is None or preferred_areas == 'all':
        return df

    # TODO(zhwu): Move type check to config validation.
    if isinstance(preferred_areas, str):
        preferred_areas = [preferred_areas]
    if not isinstance(preferred_areas, list):
        raise ValueError('Preferred area must be a string or a list of strings')

    area_filters = [f'{r.lower()}-' for r in preferred_areas]
    return df[df['Region'].str.startswith(tuple(area_filters))]


_df = common.read_catalog('aws/vms.csv',
                          update_frequency_hours=_auto_update_frequency_hours,
                          area_filter_fn=area_filter_fn)
_image_df = common.read_catalog(
    'aws/images.csv', update_frequency_hours=_auto_update_frequency_hours)


def instance_type_exists(instance_type: str) -> bool:
    return common.instance_type_exists_impl(_df, instance_type)


def validate_region_zone(
        region: Optional[str],
        zone: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    return common.validate_region_zone_impl(_df, region, zone)


def accelerator_in_region_or_zone(acc_name: str,
                                  acc_count: int,
                                  region: Optional[str] = None,
                                  zone: Optional[str] = None) -> bool:
    return common.accelerator_in_region_or_zone_impl(_df, acc_name, acc_count,
                                                     region, zone)


def get_hourly_cost(instance_type: str,
                    region: Optional[str] = None,
                    use_spot: bool = False) -> float:
    """Returns the cost, or the cheapest cost among all zones for spot."""
    return common.get_hourly_cost_impl(_df, instance_type, region, use_spot)


def get_vcpus_from_instance_type(instance_type: str) -> Optional[float]:
    return common.get_vcpus_from_instance_type_impl(_df, instance_type)


def get_accelerators_from_instance_type(
        instance_type: str) -> Optional[Dict[str, int]]:
    return common.get_accelerators_from_instance_type_impl(_df, instance_type)


def get_instance_type_for_accelerator(
    acc_name: str,
    acc_count: int,
) -> Tuple[Optional[List[str]], List[str]]:
    """
    Returns a list of instance types satisfying the required count of
    accelerators with sorted prices and a list of candidates with fuzzy search.
    """
    return common.get_instance_type_for_accelerator_impl(df=_df,
                                                         acc_name=acc_name,
                                                         acc_count=acc_count)


def get_region_zones_for_instance_type(instance_type: str,
                                       use_spot: bool) -> List['cloud.Region']:
    df = _df[_df['InstanceType'] == instance_type]
    region_list = common.get_region_zones(df, use_spot)
    # Hack: Enforce US regions are always tried first:
    #   [US regions sorted by price] + [non-US regions sorted by price]
    us_region_list = []
    other_region_list = []
    for region in region_list:
        if region.name.startswith('us-'):
            us_region_list.append(region)
        else:
            other_region_list.append(region)
    return us_region_list + other_region_list


def list_accelerators(gpus_only: bool,
                      name_filter: Optional[str],
                      case_sensitive: bool = True
                     ) -> Dict[str, List[common.InstanceTypeInfo]]:
    """Returns all instance types in AWS offering accelerators."""
    return common.list_accelerators_impl('AWS', _df, gpus_only, name_filter,
                                         case_sensitive)


def get_image_id_from_tag(tag: str, region: Optional[str]) -> Optional[str]:
    """Returns the image id from the tag."""
    return common.get_image_id_from_tag_impl(_image_df, tag, region)


def is_image_tag_valid(tag: str, region: Optional[str]) -> bool:
    """Returns whether the image tag is valid."""
    return common.is_image_tag_valid_impl(_image_df, tag, region)
