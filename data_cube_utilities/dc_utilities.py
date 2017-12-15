# Copyright 2016 United States Government as represented by the Administrator
# of the National Aeronautics and Space Administration. All Rights Reserved.
#
# Portion of this code is Copyright Geoscience Australia, Licensed under the
# Apache License, Version 2.0 (the "License"); you may not use this file
# except in compliance with the License. You may obtain a copy of the License
# at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# The CEOS 2 platform is licensed under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0.
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import gdal, osr
import numpy as np
import xarray as xr
import collections
import os
import math
import datetime
import shutil
import uuid
import rasterio


def create_cfmask_clean_mask(cfmask, no_data=-9999):
    """
    Description:
      Create a clean mask for clear land/water pixels,
      i.e. mask out shadow, snow, cloud, and no data
    -----
    Input:
      cfmask (xarray) - cf_mask from the ledaps products
    Output:
      clean_mask (boolean numpy array) - clear land/water mask
    """

    #########################
    # cfmask values:        #
    #   0 - clear           #
    #   1 - water           #
    #   2 - cloud shadow    #
    #   3 - snow            #
    #   4 - cloud           #
    #   255 - fill          #
    #########################

    clean_mask = (cfmask == 0) | (cfmask == 1)
    return clean_mask.values


def perform_timeseries_analysis(dataset_in, band_name, intermediate_product=None, no_data=-9999, operation="mean"):
    """
    Description:

    -----
    Input:
      dataset_in (xarray.DataSet) - dataset with one variable to perform timeseries on
      band_name: name of the band to create stats for.
      intermediate_product: result of this function for previous data, to be combined here
    Output:
      dataset_out (xarray.DataSet) - dataset containing
        variables: normalized_data, total_data, total_clean
    """

    assert operation in ['mean', 'max', 'min'], "Please enter a valid operation."

    data = dataset_in[band_name]
    data = data.where(data != no_data)

    processed_data_sum = data.sum('time')

    clean_data = data.notnull()

    clean_data_sum = clean_data.astype('bool').sum('time')

    dataset_out = None
    if intermediate_product is None:
        processed_data_normalized = processed_data_sum / clean_data_sum
        dataset_out = xr.Dataset(
            {
                'normalized_data': processed_data_normalized,
                'min': data.min(dim='time'),
                'max': data.max(dim='time'),
                'total_data': processed_data_sum,
                'total_clean': clean_data_sum
            },
            coords={'latitude': dataset_in.latitude,
                    'longitude': dataset_in.longitude})
    else:
        dataset_out = intermediate_product
        dataset_out['total_data'] += processed_data_sum
        dataset_out['total_clean'] += clean_data_sum
        dataset_out['normalized_data'] = dataset_out['total_data'] / dataset_out['total_clean']
        dataset_out['min'] = xr.concat([dataset_out['min'], data.min(dim='time')], dim='time').min(dim='time')
        dataset_out['max'] = xr.concat([dataset_out['max'], data.max(dim='time')], dim='time').max(dim='time')

    nan_to_num(dataset_out, 0)

    return dataset_out


def nan_to_num(dataset, number):
    for key in list(dataset.data_vars):
        dataset[key].values[np.isnan(dataset[key].values)] = number


def clear_attrs(dataset):
    """Clear out all attributes on an xarray dataset to write to disk."""
    dataset.attrs = collections.OrderedDict()
    for band in dataset:
        dataset[band].attrs = collections.OrderedDict()


def create_bit_mask(data_array, valid_bits, no_data=-9999):
    """Create a boolean bit mask from a list of valid bits

    Args:
        data_array: xarray data array to extract bit information for.
        valid_bits: array of ints representing what bits should be considered valid.
        no_data: no_data value for the data array.

    Returns:
        Boolean mask signifying valid data.

    """
    assert isinstance(valid_bits, list) and isinstance(valid_bits[0], int), "Valid bits must be a list of integer bits"
    #do bitwise and on valid mask - all zeros means no intersection e.g. invalid else return a truthy value?
    valid_mask = sum([1 << valid_bit for valid_bit in valid_bits])
    clean_mask = (data_array & valid_mask).astype('bool')
    return clean_mask.values


def add_timestamp_data_to_xr(dataset):
    """Add timestamp data to an xarray dataset using the time dimension.

    Adds both a timestamp and a human readable date int to a dataset - int32 format required.
    modifies the dataset in place.
    """
    dims_data_var = list(dataset.data_vars)[0]

    timestamp_data = np.full(dataset[dims_data_var].values.shape, 0, dtype="int32")
    date_data = np.full(dataset[dims_data_var].values.shape, 0, dtype="int32")

    for index, acq_date in enumerate(dataset.time.values.astype('M8[ms]').tolist()):
        timestamp_data[index::] = acq_date.timestamp()
        date_data[index::] = int(acq_date.strftime("%Y%m%d"))
    dataset['timestamp'] = xr.DataArray(
        timestamp_data,
        dims=('time', 'latitude', 'longitude'),
        coords={'latitude': dataset.latitude,
                'longitude': dataset.longitude,
                'time': dataset.time})
    dataset['date'] = xr.DataArray(
        date_data,
        dims=('time', 'latitude', 'longitude'),
        coords={'latitude': dataset.latitude,
                'longitude': dataset.longitude,
                'time': dataset.time})


def write_geotiff_from_xr(tif_path, dataset, bands, no_data=-9999, crs="EPSG:4326"):
    """Write a geotiff from an xarray dataset.

    Args:
        tif_path: path for the tif to be written to.
        dataset: xarray dataset
        bands: list of strings representing the bands in the order they should be written
        no_data: no_data value for the dataset
        crs: requested crs.

    """
    assert isinstance(bands, list), "Bands must a list of strings"
    assert len(bands) > 0 and isinstance(bands[0], str), "You must supply at least one band."
    with rasterio.open(
            tif_path,
            'w',
            driver='GTiff',
            height=dataset.dims['latitude'],
            width=dataset.dims['longitude'],
            count=len(bands),
            dtype=str(dataset[bands[0]].dtype),
            crs=crs,
            transform=_get_transform_from_xr(dataset),
            nodata=no_data) as dst:
        for index, band in enumerate(bands):
            dst.write(dataset[band].values, index + 1)
        dst.close()


def write_png_from_xr(png_path,
                      dataset,
                      bands,
                      png_filled_path=None,
                      fill_color='red',
                      scale=None,
                      low_res=False,
                      no_data=-9999,
                      crs="EPSG:4326"):
    """Write a rgb png from an xarray dataset.

    Args:
        png_path: path for the png to be written to.
        dataset: dataset to use for the png creation.
        bands: a list of three strings representing the bands and their order
        png_filled_path: optional png with no_data values filled
        fill_color: color to use as the no_data fill
        scale: desired scale - tuple like (0, 4000) for the upper and lower bounds

    """
    assert isinstance(bands, list), "Bands must a list of strings"
    assert len(bands) == 3 and isinstance(bands[0], str), "You must supply three string bands for a PNG."
    
    tif_path = os.path.join(os.path.dirname(png_path), str(uuid.uuid4()) + ".png")
    write_geotiff_from_xr(tif_path, dataset, bands, no_data=no_data, crs=crs)
    
    scale_string = ""
    if scale is not None and len(scale) == 2:
        scale_string = "-scale {} {} 0 255".format(scale[0], scale[1])
    elif scale is not None and len(scale) == 3:
        for index, scale_member in enumerate(scale):
            scale_string += " -scale_{} {} {} 0 255".format(index + 1, scale_member[0], scale_member[1])
    outsize_string = "-outsize 25% 25%" if low_res else ""
    cmd = "gdal_translate -ot Byte " + outsize_string + " " + scale_string + " -of PNG -b 1 -b 2 -b 3 " + tif_path + ' ' + png_path
    
    os.system(cmd)
    
    if png_filled_path is not None and fill_color is not None:
        cmd = "convert -transparent \"#000000\" " + png_path + " " + png_path
        os.system(cmd)
        cmd = "convert " + png_path + " -background " + \
            fill_color + " -alpha remove " + png_filled_path
        os.system(cmd)
    
    os.remove(tif_path)


def write_single_band_png_from_xr(png_path,
                                  dataset,
                                  band,
                                  color_scale=None,
                                  fill_color=None,
                                  interpolate=True,
                                  no_data=-9999,
                                  crs="EPSG:4326"):
    """Write a pseudocolor png from an xarray dataset.  
    Args:
        png_path: path for the png to be written to.
        dataset: dataset to use for the png creation.
        band: The band to write to a png
        png_filled_path: optional png with no_data values filled
        fill_color: color to use as the no_data fill
        color_scale: path to a color scale compatible with gdal.

    """
    assert os.path.exists(color_scale), "Color scale must be a path to a text file containing a gdal compatible scale."
    assert isinstance(band, str), "Band must be a string."
    
    tif_path = os.path.join(os.path.dirname(png_path), str(uuid.uuid4()) + ".png")
    write_geotiff_from_xr(tif_path, dataset, [band], no_data=no_data, crs=crs)
    
    interpolation_settings = "-nearest_color_entry" if not interpolate else ""
    
    cmd = "gdaldem color-relief -of PNG " + interpolation_settings + " -b 1 " + tif_path + " " + \
        color_scale + " " + png_path
    os.system(cmd)
    
    if fill_color is not None:
        cmd = "convert -transparent \"#FFFFFF\" " + \
            png_path + " " + png_path
        os.system(cmd)
        if fill_color is not None and fill_color != "transparent":
            cmd = "convert " + png_path + " -background " + \
                fill_color + " -alpha remove " + png_path
            os.system(cmd)
    
    os.remove(tif_path)

def _get_transform_from_xr(dataset):
    """Create a geotransform from an xarray dataset.
    """

    from rasterio.transform import from_bounds
    geotransform = from_bounds(dataset.longitude[0], dataset.latitude[-1], dataset.longitude[-1], dataset.latitude[0],
                               len(dataset.longitude), len(dataset.latitude))
    return geotransform


# Break the list l into n sized chunks.
def chunks(l, n):
    for i in range(0, len(l), n):
        yield l[i:i + n]
