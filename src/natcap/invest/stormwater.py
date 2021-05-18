"""Stormwater Retention."""
import logging
import math
import os

import numpy
from osgeo import gdal, ogr, osr
import pygeoprocessing
import taskgraph

from . import validation
from . import utils

LOGGER = logging.getLogger(__name__)

# a constant nodata value to use for intermediates and outputs
FLOAT_NODATA = -1
UINT8_NODATA = 255
UINT16_NODATA = 65535

ARGS_SPEC = {
    "model_name": "Stormwater Retention",
    "module": __name__,
    "userguide_html": "stormwater.html",
    "args_with_spatial_overlap": {
        "spatial_keys": ["lulc_path", "soil_group_path", "precipitation_path",
                         "road_centerlines_path", "aggregate_areas_path"],
        "different_projections_ok": True
    },
    "args": {
        "workspace_dir": validation.WORKSPACE_SPEC,
        "results_suffix": validation.SUFFIX_SPEC,
        "n_workers": validation.N_WORKERS_SPEC,
        "lulc_path": {
            "type": "raster",
            "required": True,
            "about": (
                "A map of land use/land cover classes in the area of "
                "interest"),
            "name": "Land use/land cover",
            "validation_options": {
                "projected": True
            }
        },
        "soil_group_path": {
            "type": "raster",
            "required": True,
            "about": (
                "Map of hydrologic soil groups, where pixel values 1, 2, 3, "
                "and 4 correspond to groups A, B, C, and D respectively"),
            "name": "Soil groups"
        },
        "precipitation_path": {
            "type": "raster",
            "required": True,
            "about": "Map of total annual precipitation",
            "name": "Precipitation"
        },
        "biophysical_table": {
            "type": "csv",
            "required": True,
            "about": "biophysical table",
            "name": "Biophysical table"
        },
        "adjust_retention_ratios": {
            "type": "boolean",
            "required": True,
            "about": (
                "If true, adjust retention ratios. The adjustment algorithm "
                "accounts for drainage effects of nearby impervious surfaces "
                "which are directly connected to artifical urban drainage "
                "channels (typically roads, parking lots, etc.) Connected "
                "impervious surfaces are indicated by the is_impervious column"
                "in the biophysical table and/or the road centerlines vector."),
            "name": "Adjust retention ratios"
        },
        "retention_radius": {
            "type": "number",
            "required": "adjust_retention_ratios",
            "about": (
                "Radius around each pixel to adjust retention ratios. For the "
                "adjustment algorithm, a pixel is 'near' a connected "
                "impervious surface if its centerpoint is within this radius "
                "of connected-impervious LULC and/or a road centerline."),
            "name": "Retention radius"
        },
        "road_centerlines_path": {
            "type": "vector",
            "required": "adjust_retention_ratios",
            "about": "Map of road centerlines",
            "name": "Road centerlines"
        },
        "aggregate_areas_path": {
            "type": "vector",
            "required": False,
            "about": (
                "Areas over which to aggregate results (typically watersheds "
                "or sewersheds). The aggregated data are: average retention "
                "ratio and total retention volume; average infiltration ratio "
                "and total infiltration volume if infiltration data was "
                "provided; total retention value if replacement cost was "
                "provided; and total avoided pollutant load for each "
                "pollutant provided."),
            "name": "Aggregate areas"
        },
        "replacement_cost": {
            "type": "number",
            "required": False,
            "about": "Replacement cost of stormwater retention devices",
            "name": "Replacement cost"
        }
    }
}

INTERMEDIATE_OUTPUTS = {
    'lulc_aligned_path': 'lulc_aligned.tif',
    'soil_group_aligned_path': 'soil_group_aligned.tif',
    'precipitation_aligned_path': 'precipitation_aligned.tif',
    'reprojected_centerlines_path': 'reprojected_centerlines.gpkg',
    'rasterized_centerlines_path': 'rasterized_centerlines.tif',
    'impervious_lulc_path': 'is_impervious_lulc.tif',
    'retention_ratio_path': 'retention_ratio.tif',
    'adjusted_retention_ratio_path': 'adjusted_retention_ratio.tif',
    'x_coords_path': 'x_coords.tif',
    'y_coords_path': 'y_coords.tif',
    'road_distance_path': 'road_distance.tif',
    'search_kernel_path': 'search_kernel.tif',
    'impervious_lulc_distance_path': 'impervious_lulc_distance.tif',
    'near_impervious_lulc_path': 'near_impervious_lulc.tif',
    'near_road_path': 'near_road.tif',
    'ratio_n_values_path': 'ratio_n_values.tif',
    'ratio_sum_path': 'ratio_sum.tif',
    'ratio_average_path': 'ratio_average.tif'
}

FINAL_OUTPUTS = {
    'reprojected_aggregate_areas_path': 'aggregate_data.gpkg',
    'retention_volume_path': 'retention_volume.tif',
    'infiltration_ratio_path': 'infiltration_ratio.tif',
    'infiltration_volume_path': 'infiltration_volume.tif',
    'retention_value_path': 'retention_value.tif'
}


def execute(args):
    """Execute the stormwater model.

    Args:
        args['workspace_dir'] (str): path to a directory to write intermediate
            and final outputs. May already exist or not.
        args['results_suffix'] (str, optional): string to append to all output
            file names from this model run
        args['n_workers'] (int): if present, indicates how many worker
            processes should be used in parallel processing. -1 indicates
            single process mode, 0 is single process but non-blocking mode,
            and >= 1 is number of processes.
        args['lulc_path'] (str): path to LULC raster
        args['soil_group_path'] (str): path to soil group raster, where pixel
            values 1, 2, 3, 4 correspond to groups A, B, C, D
        args['precipitation_path'] (str): path to raster of total annual
            precipitation in millimeters
        args['biophysical_table'] (str): path to biophysical table with columns
            'lucode', 'EMC_x' (event mean concentration mg/L) for each
            pollutant x, 'RC_y' (retention coefficient) and 'IR_y'
            (infiltration coefficient) for each soil group y, and
            'is_impervious' if args['adjust_retention_ratios'] is True
        args['adjust_retention_ratios'] (bool): If True, apply retention ratio
            adjustment algorithm.
        args['retention_radius'] (float): If args['adjust_retention_ratios']
            is True, use this radius in the adjustment algorithm.
        args['road_centerliens_path'] (str): Path to linestring vector of road
            centerlines. Only used if args['adjust_retention_ratios'] is True.
        args['aggregate_areas_path'] (str): Optional path to polygon vector of
            areas to aggregate results over.
        args['replacement_cost'] (float): Cost to replace stormwater retention
            devices in units currency per cubic meter

    Returns:
        None
    """
    # set up files and directories
    suffix = utils.make_suffix_string(args, 'results_suffix')
    output_dir = args['workspace_dir']
    intermediate_dir = os.path.join(output_dir, 'intermediate')
    cache_dir = os.path.join(intermediate_dir, 'cache_dir')
    utils.make_directories(
        [args['workspace_dir'], intermediate_dir, cache_dir])
    files = utils.build_file_registry(
        [(INTERMEDIATE_OUTPUTS, intermediate_dir),
         (FINAL_OUTPUTS, output_dir)], suffix)

    task_graph = taskgraph.TaskGraph(cache_dir, int(args.get('n_workers', -1)))

    # get the necessary base raster info
    source_lulc_raster_info = pygeoprocessing.get_raster_info(
        args['lulc_path'])
    pixel_size = source_lulc_raster_info['pixel_size']
    pixel_area = abs(pixel_size[0] * pixel_size[1])

    lulc_nodata = source_lulc_raster_info['nodata'][0]
    precipitation_nodata = pygeoprocessing.get_raster_info(
        args['precipitation_path'])['nodata'][0]
    soil_group_nodata = pygeoprocessing.get_raster_info(
        args['soil_group_path'])['nodata'][0]

    # Align all three input rasters to the same projection
    align_inputs = [args['lulc_path'],
                    args['soil_group_path'], args['precipitation_path']]
    align_outputs = [
        files['lulc_aligned_path'],
        files['soil_group_aligned_path'],
        files['precipitation_aligned_path']]
    align_task = task_graph.add_task(
        func=pygeoprocessing.align_and_resize_raster_stack,
        args=(
            align_inputs,
            align_outputs,
            ['near' for _ in align_inputs],
            pixel_size,
            'intersection'),
        kwargs={'raster_align_index': 0},
        target_path_list=align_outputs,
        task_name='align input rasters')

    # Build a lookup dictionary mapping each LULC code to its row
    biophysical_dict = utils.build_lookup_from_csv(
        args['biophysical_table'], 'lucode')
    # sort the LULC codes upfront because we use the sorted list in multiple
    # places. it's more efficient to do this once.
    sorted_lucodes = sorted(biophysical_dict)

    # convert the nested dictionary in to a 2D array where rows are LULC codes
    # in sorted order and columns correspond to soil groups in order
    # this facilitates efficiently looking up the ratio values with numpy
    #
    # Biophysical table has runoff coefficents so subtract
    # from 1 to get retention coefficient.
    # add a placeholder in column 0 so that the soil groups 1, 2, 3, 4 line
    # up with their indices in the array. this is more efficient than
    # decrementing the whole soil group array by 1.
    retention_ratio_array = numpy.array([
        [1 - biophysical_dict[lucode][f'rc_{soil_group}']
            for soil_group in ['a', 'b', 'c', 'd']
        ] for lucode in sorted_lucodes
    ], dtype=numpy.float32)

    # Calculate stormwater retention ratio and volume from
    # LULC, soil groups, biophysical data, and precipitation
    retention_ratio_task = task_graph.add_task(
        func=lookup_ratios,
        args=(
            files['lulc_aligned_path'],
            lulc_nodata,
            files['soil_group_aligned_path'],
            soil_group_nodata,
            retention_ratio_array,
            sorted_lucodes,
            files['retention_ratio_path']),
        target_path_list=[files['retention_ratio_path']],
        dependent_task_list=[align_task],
        task_name='calculate stormwater retention ratio'
    )

    # (Optional) adjust stormwater retention ratio using roads
    if args['adjust_retention_ratios']:
        # in raster coord system units
        radius = float(args['retention_radius'])
        # boolean mapping for each LULC code whether it's impervious
        is_impervious_map = {
            lucode: 1 if biophysical_dict[lucode]['is_impervious'] else 0
            for lucode in biophysical_dict}

        reproject_roads_task = task_graph.add_task(
            func=pygeoprocessing.reproject_vector,
            args=(
                args['road_centerlines_path'],
                source_lulc_raster_info['projection_wkt'],
                files['reprojected_centerlines_path']),
            kwargs={'driver_name': 'GPKG'},
            target_path_list=[files['reprojected_centerlines_path']],
            task_name='reproject road centerlines vector to match rasters',
            dependent_task_list=[])

        # pygeoprocessing.rasterize expects the target raster to already exist
        make_raster_task = task_graph.add_task(
            func=pygeoprocessing.new_raster_from_base,
            args=(
                files['lulc_aligned_path'],
                files['rasterized_centerlines_path'],
                gdal.GDT_Byte,
                [UINT8_NODATA]),
            target_path_list=[files['rasterized_centerlines_path']],
            task_name='create raster to pass to pygeoprocessing.rasterize',
            dependent_task_list=[align_task])

        rasterize_centerlines_task = task_graph.add_task(
            func=pygeoprocessing.rasterize,
            args=(
                files['reprojected_centerlines_path'],
                files['rasterized_centerlines_path'],
                [1]),
            target_path_list=[files['rasterized_centerlines_path']],
            task_name='rasterize road centerlines vector',
            dependent_task_list=[make_raster_task, reproject_roads_task])

        # Make a boolean raster showing which pixels are within the given
        # radius of a road centerline
        near_road_task = task_graph.add_task(
            func=is_near,
            args=(
                files['rasterized_centerlines_path'],
                radius / pixel_size[0],  # convert the radius to pixels
                files['road_distance_path'],
                files['near_road_path']),
            target_path_list=[
                files['road_distance_path'],
                files['near_road_path']],
            task_name='find pixels within radius of road centerlines',
            dependent_task_list=[rasterize_centerlines_task])

        # Make a boolean raster indicating which pixels are directly
        # connected impervious LULC type
        impervious_lulc_task = task_graph.add_task(
            func=pygeoprocessing.reclassify_raster,
            args=(
                (files['lulc_aligned_path'], 1),
                is_impervious_map,
                files['impervious_lulc_path'],
                gdal.GDT_Byte,
                UINT8_NODATA),
            target_path_list=[files['impervious_lulc_path']],
            task_name='calculate binary impervious lulc raster',
            dependent_task_list=[align_task]
        )

        # Make a boolean raster showing which pixels are within the given
        # radius of impervious land cover
        near_impervious_lulc_task = task_graph.add_task(
            func=is_near,
            args=(
                files['impervious_lulc_path'],
                radius / pixel_size[0],  # convert the radius to pixels
                files['impervious_lulc_distance_path'],
                files['near_impervious_lulc_path']),
            target_path_list=[
                files['impervious_lulc_distance_path'],
                files['near_impervious_lulc_path']],
            task_name='find pixels within radius of impervious lulc',
            dependent_task_list=[impervious_lulc_task])

        average_ratios_task = task_graph.add_task(
            func=raster_average,
            args=(
                files['retention_ratio_path'],
                radius,
                files['search_kernel_path'],
                files['ratio_average_path']),
            target_path_list=[files['ratio_average_path']],
            task_name='average retention ratios within radius',
            dependent_task_list=[retention_ratio_task])

        # Using the averaged retention ratio raster and boolean
        # "within radius" rasters, adjust the retention ratios
        adjust_retention_ratio_task = task_graph.add_task(
            func=pygeoprocessing.raster_calculator,
            args=([
                (files['retention_ratio_path'], 1),
                (files['ratio_average_path'], 1),
                (files['near_impervious_lulc_path'], 1),
                (files['near_road_path'], 1)],
                adjust_op,
                files['adjusted_retention_ratio_path'],
                gdal.GDT_Float32,
                FLOAT_NODATA),
            target_path_list=[files['adjusted_retention_ratio_path']],
            task_name='adjust stormwater retention ratio',
            dependent_task_list=[retention_ratio_task, average_ratios_task,
                                 near_impervious_lulc_task, near_road_task])

        final_retention_ratio_path = files['adjusted_retention_ratio_path']
        final_retention_ratio_task = adjust_retention_ratio_task
    else:
        final_retention_ratio_path = files['retention_ratio_path']
        final_retention_ratio_task = retention_ratio_task

    # Calculate stormwater retention volume from ratios and precipitation
    retention_volume_task = task_graph.add_task(
        func=pygeoprocessing.raster_calculator,
        args=([
            (final_retention_ratio_path, 1),
            (files['precipitation_aligned_path'], 1),
            (precipitation_nodata, 'raw'),
            (pixel_area, 'raw')],
            volume_op,
            files['retention_volume_path'],
            gdal.GDT_Float32,
            FLOAT_NODATA),
        target_path_list=[files['retention_volume_path']],
        dependent_task_list=[align_task, final_retention_ratio_task],
        task_name='calculate stormwater retention volume'
    )
    aggregation_task_dependencies = [retention_volume_task]
    data_to_aggregate = [
        # tuple of (raster path, output field name, op) for aggregation
        (files['retention_ratio_path'], 'RR_mean', 'mean'),
        (files['retention_volume_path'], 'RV_sum', 'sum')]

    # (Optional) Calculate stormwater infiltration ratio and volume from
    # LULC, soil groups, biophysical table, and precipitation
    if 'ir_a' in next(iter(biophysical_dict.values())):
        LOGGER.info('Infiltration data detected in biophysical table. '
                    'Will calculate infiltration ratio and volume rasters.')
        infiltration_ratio_array = numpy.array([
            [biophysical_dict[lucode][f'ir_{soil_group}']
                for soil_group in ['a', 'b', 'c', 'd']
             ] for lucode in sorted_lucodes
        ], dtype=numpy.float32)
        infiltration_ratio_task = task_graph.add_task(
            func=lookup_ratios,
            args=(
                files['lulc_aligned_path'],
                lulc_nodata,
                files['soil_group_aligned_path'],
                soil_group_nodata,
                infiltration_ratio_array,
                sorted_lucodes,
                files['infiltration_ratio_path']),
            target_path_list=[files['infiltration_ratio_path']],
            dependent_task_list=[align_task],
            task_name='calculate stormwater infiltration ratio')

        infiltration_volume_task = task_graph.add_task(
            func=pygeoprocessing.raster_calculator,
            args=([
                (files['infiltration_ratio_path'], 1),
                (files['precipitation_aligned_path'], 1),
                (precipitation_nodata, 'raw'),
                (pixel_area, 'raw')],
                volume_op,
                files['infiltration_volume_path'],
                gdal.GDT_Float32,
                FLOAT_NODATA),
            target_path_list=[files['infiltration_volume_path']],
            dependent_task_list=[align_task, infiltration_ratio_task],
            task_name='calculate stormwater retention volume'
        )
        aggregation_task_dependencies.append(infiltration_volume_task)
        data_to_aggregate.append(
            (files['infiltration_ratio_path'], 'IR_mean', 'mean'))
        data_to_aggregate.append(
            (files['infiltration_volume_path'], 'IV_sum', 'sum'))

    # get all EMC columns from an arbitrary row in the dictionary
    # strip the first four characters off 'EMC_pollutant' to get pollutant name
    pollutants = [key[4:] for key in next(iter(biophysical_dict.values()))
                  if key.startswith('emc_')]
    LOGGER.debug(f'Pollutants found in biophysical table: {pollutants}')

    # Calculate avoided pollutant load for each pollutant from retention volume
    # and biophysical table EMC value
    avoided_load_paths = []
    for pollutant in pollutants:
        # one output raster for each pollutant
        avoided_pollutant_load_path = os.path.join(
            output_dir, f'avoided_pollutant_load_{pollutant}{suffix}.tif')
        avoided_load_paths.append(avoided_pollutant_load_path)
        # make an array mapping each LULC code to the pollutant EMC value
        emc_array = numpy.array(
            [biophysical_dict[lucode][f'emc_{pollutant}']
                for lucode in sorted_lucodes], dtype=numpy.float32)

        avoided_load_task = task_graph.add_task(
            func=pygeoprocessing.raster_calculator,
            args=([
                (files['lulc_aligned_path'], 1),
                (lulc_nodata, 'raw'),
                (files['retention_volume_path'], 1),
                (sorted_lucodes, 'raw'),
                (emc_array, 'raw')],
                avoided_pollutant_load_op,
                avoided_pollutant_load_path,
                gdal.GDT_Float32,
                FLOAT_NODATA),
            target_path_list=[avoided_pollutant_load_path],
            dependent_task_list=[retention_volume_task],
            task_name=f'calculate avoided pollutant {pollutant} load'
        )
        aggregation_task_dependencies.append(avoided_load_task)
        data_to_aggregate.append(
            (avoided_pollutant_load_path, f'avoided_{pollutant}', 'sum'))

    # (Optional) Do valuation if a replacement cost is defined
    # you could theoretically have a cost of 0 which should be allowed
    if 'replacement_cost' in args and args['replacement_cost'] not in [
            None, '']:
        valuation_task = task_graph.add_task(
            func=pygeoprocessing.raster_calculator,
            args=([
                (files['retention_volume_path'], 1),
                (float(args['replacement_cost']), 'raw')],
                retention_value_op,
                files['retention_value_path'],
                gdal.GDT_Float32,
                FLOAT_NODATA),
            target_path_list=[files['retention_value_path']],
            dependent_task_list=[retention_volume_task],
            task_name='calculate stormwater retention value'
        )
        aggregation_task_dependencies.append(valuation_task)
        data_to_aggregate.append(
            (files['retention_value_path'], 'val_sum', 'sum'))

    # (Optional) Aggregate to watersheds if an aggregate vector is defined
    if 'aggregate_areas_path' in args and args['aggregate_areas_path']:
        reproject_aggregate_areas_task = task_graph.add_task(
            func=pygeoprocessing.reproject_vector,
            args=(
                args['aggregate_areas_path'],
                source_lulc_raster_info['projection_wkt'],
                files['reprojected_aggregate_areas_path']),
            kwargs={'driver_name': 'GPKG'},
            target_path_list=[files['reprojected_aggregate_areas_path']],
            task_name='reproject aggregate areas vector to match rasters',
            dependent_task_list=[]
        )
        aggregation_task_dependencies.append(reproject_aggregate_areas_task)
        _ = task_graph.add_task(
            func=aggregate_results,
            args=(
                files['reprojected_aggregate_areas_path'],
                data_to_aggregate),
            target_path_list=[files['reprojected_aggregate_areas_path']],
            dependent_task_list=aggregation_task_dependencies,
            task_name='aggregate data over polygons'
        )

    task_graph.close()
    task_graph.join()


def lookup_ratios(lulc_path, lulc_nodata, soil_group_path, soil_group_nodata,
    ratio_lookup, sorted_lucodes, output_path):
    """Look up retention/infiltration ratios from LULC codes and soil groups.

    Args:
        lulc_array (numpy.ndarray): 2D array of LULC codes
        lulc_nodata (int): nodata value for the LULC array
        soil_group_array (numpy.ndarray): 2D array with the same shape as
            ``lulc_array``. Values in {1, 2, 3, 4} corresponding to soil
            groups A, B, C, and D.
        soil_group_nodata (int): nodata value for the soil group array
        ratio_lookup (numpy.ndarray): 2D array where rows correspond to
            sorted LULC codes and the 4 columns correspond to soil groups
            A, B, C, D in order. Shape: (number of lulc codes, 4).
        sorted_lucodes (list[int]): List of LULC codes sorted from smallest
            to largest. These correspond to the rows of ``ratio_lookup``.
        output_path (str): path to a raster to write out the result. has the
            same shape as the lulc and soil group rasters. Each value is the
            corresponding ratio for that LULC code x soil group pair.

    Returns:
        None
    """
    # insert a column on the left side of the array so that the soil
    # group codes 1-4 line up with their indexes. this is faster than
    # decrementing every value in a large raster.
    ratio_lookup = numpy.insert(ratio_lookup, 0,
        numpy.zeros(ratio_lookup.shape[0]), axis=1)

    def ratio_op(lulc_array, soil_group_array):
        output_ratio_array = numpy.full(lulc_array.shape, FLOAT_NODATA,
            dtype=numpy.float32)
        valid_mask = ((lulc_array != lulc_nodata) &
                      (soil_group_array != soil_group_nodata))
        # the index of each lucode in the sorted lucodes array
        lulc_index = numpy.digitize(lulc_array[valid_mask], sorted_lucodes,
                                    right=True)
        output_ratio_array[valid_mask] = ratio_lookup[lulc_index,
            soil_group_array[valid_mask]]
        return output_ratio_array

    pygeoprocessing.raster_calculator(
        [(lulc_path, 1), (soil_group_path, 1)],
        ratio_op,
        output_path,
        gdal.GDT_Float32,
        FLOAT_NODATA)


def volume_op(ratio_array, precip_array, precip_nodata, pixel_area):
    """Calculate stormwater volumes from precipitation and ratios.

    Args:
        ratio_array (numpy.ndarray): 2D array of stormwater ratios. Assuming
            that its nodata value is the global FLOAT_NODATA.
        precip_array (numpy.ndarray): 2D array of precipitation amounts
            in millimeters/year
        precip_nodata (float): nodata value for the precipitation array
        pixel_area (float): area of each pixel in m^2

    Returns:
        2D numpy.ndarray of precipitation volumes in m^3/year
    """
    volume_array = numpy.full(ratio_array.shape, FLOAT_NODATA,
                              dtype=numpy.float32)
    valid_mask = ~numpy.isclose(ratio_array, FLOAT_NODATA)
    if precip_nodata:
        valid_mask &= ~numpy.isclose(precip_array, precip_nodata)

    # precipitation (mm/yr) * pixel area (m^2) *
    # 0.001 (m/mm) * ratio = volume (m^3/yr)
    volume_array[valid_mask] = (
        precip_array[valid_mask] *
        ratio_array[valid_mask] *
        pixel_area * 0.001)
    return volume_array


def avoided_pollutant_load_op(lulc_array, lulc_nodata, retention_volume_array,
    sorted_lucodes, emc_array):
    """Calculate avoided pollutant loads from LULC codes retention volumes.

    Args:
        lulc_array (numpy.ndarray): 2D array of LULC codes
        lulc_nodata (int): nodata value for the LULC array
        retention_volume_array (numpy.ndarray): 2D array of stormwater
            retention volumes, with the same shape as ``lulc_array``. It is
            assumed that the retention volume nodata value is the global
            FLOAT_NODATA.
        sorted_lucodes (numpy.ndarray): 1D array of the LULC codes in order
            from smallest to largest
        emc_array (numpy.ndarray): 1D array of pollutant EMC values for each
            lucode. ``emc_array[i]`` is the EMC for the LULC class at
            ``sorted_lucodes[i]``.

    Returns:
        2D numpy.ndarray with the same shape as ``lulc_array``.
        Each value is the avoided pollutant load on that pixel in kg/yr,
        or FLOAT_NODATA if any of the inputs have nodata on that pixel.
    """
    load_array = numpy.full(
        lulc_array.shape, FLOAT_NODATA, dtype=numpy.float32)
    valid_mask = (
        (lulc_array != lulc_nodata) &
        ~numpy.isclose(retention_volume_array, FLOAT_NODATA))

    # bin each value in the LULC array such that
    # lulc_array[i,j] == sorted_lucodes[lulc_index[i,j]]. thus,
    # emc_array[lulc_index[i,j]] is the EMC for the lucode at lulc_array[i,j]
    lulc_index = numpy.digitize(lulc_array, sorted_lucodes, right=True)
    # EMC for pollutant (mg/L) * 1000 (L/m^3) * 0.000001 (kg/mg) *
    # retention (m^3/yr) = pollutant load (kg/yr)
    load_array[valid_mask] = (emc_array[lulc_index][valid_mask] *
                              0.001 * retention_volume_array[valid_mask])
    return load_array


def retention_value_op(retention_volume_array, replacement_cost):
    """Multiply retention volumes by the retention replacement cost.

    Args:
        retention_volume_array (numpy.ndarray): 2D array of retention volumes.
            Assumes that the retention volume nodata value is the global
            FLOAT_NODATA.
        replacement_cost (float): Replacement cost per cubic meter of water

    Returns:
        numpy.ndarray of retention values with the same dimensions as the input
    """
    value_array = numpy.full(retention_volume_array.shape, FLOAT_NODATA,
                             dtype=numpy.float32)
    valid_mask = ~numpy.isclose(retention_volume_array, FLOAT_NODATA)

    # retention (m^3/yr) * replacement cost ($/m^3) = retention value ($/yr)
    value_array[valid_mask] = (
        retention_volume_array[valid_mask] * replacement_cost)
    return value_array


def adjust_op(ratio_array, avg_ratio_array, near_impervious_lulc_array,
              near_road_array):
    """Apply the retention ratio adjustment algorithm to an array of ratios.

    This is meant to be used with raster_calculator. Assumes that the nodata
    value for all four input arrays is the global FLOAT_NODATA.

    Args:
        ratio_array (numpy.ndarray): 2D array of stormwater retention ratios
        avg_ratio_array (numpy.ndarray): 2D array of averaged ratios
        near_impervious_lulc_array (numpy.ndarray): 2D boolean array where 1
            means this pixel is near a directly-connected LULC area
        near_road_array (numpy.ndarray): 2D boolean array where 1
            means this pixel is near a road centerline

    Returns:
        2D numpy array of adjusted retention ratios. Has the same shape as
        ``retention_ratio_array``.
    """
    adjusted_ratio_array = numpy.full(ratio_array.shape, FLOAT_NODATA,
                                      dtype=numpy.float32)
    adjustment_factor_array = numpy.full(ratio_array.shape, FLOAT_NODATA,
                                         dtype=numpy.float32)
    valid_mask = (
        ~numpy.isclose(ratio_array, FLOAT_NODATA) &
        ~numpy.isclose(avg_ratio_array, FLOAT_NODATA) &
        (near_impervious_lulc_array != UINT8_NODATA) &
        (near_road_array != UINT8_NODATA))

    # adjustment factor:
    # - 0 if any of the nearby pixels are impervious/connected;
    # - average of nearby pixels, otherwise
    is_not_impervious = ~(
        near_impervious_lulc_array[valid_mask] |
        near_road_array[valid_mask]).astype(bool)
    adjustment_factor_array[valid_mask] = (avg_ratio_array[valid_mask] *
                                           is_not_impervious)

    adjustment_factor_array[valid_mask] = (
        avg_ratio_array[valid_mask] * ~(
            near_impervious_lulc_array[valid_mask] |
            near_road_array[valid_mask]
        ).astype(bool))

    # equation 2-4: Radj_ij = R_ij + (1 - R_ij) * C_ij
    adjusted_ratio_array[valid_mask] = (ratio_array[valid_mask] +
        (1 - ratio_array[valid_mask]) * adjustment_factor_array[valid_mask])
    return adjusted_ratio_array


def aggregate_results(aggregate_areas_path, aggregations):
    """Aggregate outputs into regions of interest.

    Args:
        aggregate_areas_path (str): path to vector of polygon(s) to aggregate
            over. This should be a copy that it's okay to modify.
        aggregations (list[tuple(str,str,str)]): list of tuples describing the
            datasets to aggregate. Each tuple has 3 items. The first is the
            path to a raster to aggregate. The second is the field name for
            this aggregated data in the output vector. The third is either
            'mean' or 'sum' indicating the aggregation to perform.

    Returns:
        None
    """
    # create a copy of the aggregate areas vector to write to
    aggregate_vector = gdal.OpenEx(aggregate_areas_path, gdal.GA_Update)
    aggregate_layer = aggregate_vector.GetLayer()

    for raster_path, field_id, op in aggregations:
        # aggregate the raster by the vector region(s)
        aggregate_stats = pygeoprocessing.zonal_statistics(
            (raster_path, 1), aggregate_areas_path)

        # set up the field to hold the aggregate data
        aggregate_field = ogr.FieldDefn(field_id, ogr.OFTReal)
        aggregate_field.SetWidth(24)
        aggregate_field.SetPrecision(11)
        aggregate_layer.CreateField(aggregate_field)
        aggregate_layer.ResetReading()

        # save the aggregate data to the field for each feature
        for feature in aggregate_layer:
            feature_id = feature.GetFID()
            if op == 'mean':
                pixel_count = aggregate_stats[feature_id]['count']
                try:
                    value = (aggregate_stats[feature_id]['sum'] / pixel_count)
                except ZeroDivisionError:
                    LOGGER.warning(
                        f'Polygon {feature_id} does not overlap {raster_path}')
                    value = 0.0
            elif op == 'sum':
                value = aggregate_stats[feature_id]['sum']
            feature.SetField(field_id, float(value))
            aggregate_layer.SetFeature(feature)

    # save the aggregate vector layer and clean up references
    aggregate_layer.SyncToDisk()
    aggregate_layer = None
    gdal.Dataset.__swig_destroy__(aggregate_vector)
    aggregate_vector = None


def is_near(input_path, radius, distance_path, out_path):
    """Make binary raster of which pixels are within a radius of a '1' pixel.

    Args:
        input_path (str): path to a binary raster where '1' pixels are what
            we're measuring distance to, in this case roads/impervious areas
        radius (float): distance in pixels which is considered "near".
            pixels this distance or less from a '1' pixel are marked '1' in
            the output. Distances are measured centerpoint to centerpoint.
        distance_path (str): path to write out the raster of distances
        out_path (str): path to write out the final thresholded raster.
            Pixels marked '1' are near to a '1' pixel in the input, pixels
            marked '0' are not.

    Returns:
        None
    """
    # Calculate the distance from each pixel to the nearest '1' pixel
    pygeoprocessing.distance_transform_edt(
        (input_path, 1),
        distance_path)

    def lte_threshold_op(array, threshold):
        """Binary array of elements less than or equal to the threshold."""
        # no need to mask nodata because distance_transform_edt doesn't
        # output any nodata pixels
        return array <= threshold

    # Threshold that to a binary array so '1' means it's within the radius
    pygeoprocessing.raster_calculator(
        [(distance_path, 1), (radius, 'raw')],
        lte_threshold_op,
        out_path,
        gdal.GDT_Byte,
        UINT8_NODATA)


def make_search_kernel(raster_path, radius):
    """Make a search kernel for a raster that marks pixels within a radius.

    Args:
        raster_path (str): path to a raster to make kernel for
        radius (float): distance around each pixel's centerpoint to search
            in raster coordinate system units

    Returns:
        2D boolean numpy.ndarray. '1' pixels are within ``radius`` of the
        center pixel, measured centerpoint-to-centerpoint. '0' pixels are
        outside the radius. The array dimensions are as small as possible
        while still including the entire radius.
    """
    raster_info = pygeoprocessing.get_raster_info(raster_path)
    pixel_radius = radius / raster_info['pixel_size'][0]
    pixel_margin = math.floor(pixel_radius)
    # the search kernel is just large enough to contain all pixels that
    # *could* be within the radius of the center pixel
    search_kernel_shape = tuple([pixel_margin * 2 + 1] * 2)
    # arrays of the column index and row index of each pixel
    col_indices, row_indices = numpy.indices(search_kernel_shape)
    # adjust them so that (0, 0) is the center pixel
    col_indices -= pixel_margin
    row_indices -= pixel_margin
    # hypotenuse_i = sqrt(col_indices_i**2 + row_indices_i**2) for each pixel i
    hypotenuse = numpy.hypot(col_indices, row_indices)
    # boolean kernel where 1=pixel centerpoint is within the radius of the
    # center pixel's centerpoint
    search_kernel = numpy.array(hypotenuse <= pixel_radius, dtype=numpy.uint8)
    LOGGER.debug(
        f'Search kernel for {raster_path} with radius {radius}:'
        f'\n{search_kernel}')
    return search_kernel


def raster_average(raster_path, radius, kernel_path, out_path):
    """Average pixel values within a radius.

    Make a search kernel where a pixel has '1' if its centerpoint is within
    the radius of the center pixel's centerpoint.
    For each pixel in a raster, center the search kernel on top of it. Then
    its "neighborhood" includes all the pixels that are below a '1' in the
    search kernel. Add up the neighborhood pixel values and divide by how
    many there are.

    This accounts for edge pixels and nodata pixels. For instance, if the
    kernel covers a 3x3 pixel area centered on each pixel, most pixels will
    have 9 valid pixels in their neighborhood, most edge pixels will have 6,
    and most corner pixels will have 4. Edge and nodata pixels in the
    neighborhood don't count towards the total (denominator in the average).

    Args:
        raster_path (str): path to the raster file to average
        radius (float): distance to average around each pixel's centerpoint in
            raster coordinate system units
        kernel_path (str): path to write out the search kernel raster, an
            intermediate output required by pygeoprocessing.convolve_2d
        out_path (str): path to write out the averaged raster output

    Returns:
        None
    """
    search_kernel = make_search_kernel(raster_path, radius)

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    projection_wkt = srs.ExportToWkt()
    pygeoprocessing.numpy_array_to_raster(
        # float32 here to avoid pygeoprocessing bug issue #180
        search_kernel.astype(numpy.float32),
        FLOAT_NODATA,
        (20, -20),
        (0, 0),
        projection_wkt,
        kernel_path)

    # convolve the signal (input raster) with the kernel and normalize
    # this is equivalent to taking an average of each pixel's neighborhood
    pygeoprocessing.convolve_2d(
        (raster_path, 1),
        (kernel_path, 1),
        out_path,
        # pixels with nodata or off the edge of the raster won't count towards
        # the sum or the number of values to normalize by
        ignore_nodata_and_edges=True,
        # divide by number of valid pixels in the kernel (averaging)
        normalize_kernel=True,
        # output will have nodata where ratio_path has nodata
        mask_nodata=True,
        target_datatype=gdal.GDT_Float32,
        target_nodata=FLOAT_NODATA)


@validation.invest_validator
def validate(args, limit_to=None):
    """Validate args to ensure they conform to `execute`'s contract.

    Args:
        args (dict): dictionary of key(str)/value pairs where keys and
            values are specified in `execute` docstring.
        limit_to (str): (optional) if not None indicates that validation
            should only occur on the args[limit_to] value. The intent that
            individual key validation could be significantly less expensive
            than validating the entire `args` dictionary.

    Returns:
        list of ([invalid key_a, invalid_keyb, ...], 'warning/error message')
            tuples. Where an entry indicates that the invalid keys caused
            the error message in the second part of the tuple. This should
            be an empty list if validation succeeds.
    """
    return validation.validate(args, ARGS_SPEC['args'],
                               ARGS_SPEC['args_with_spatial_overlap'])
