import copy
import unittest

import bpy
from bpy import data as D  # noqa
from bpy import types as T  # noqa
from mixer.blender_data.blenddata import BlendData
from mixer.blender_data.proxy import BpyBlendProxy, BpyIDProxy, BpyIDRefProxy, BpyPropertyGroupProxy, SoaElement
from mixer.blender_data.tests.utils import test_blend_file

from mixer.blender_data.filter import (
    Context,
    TypeFilterOut,
    default_context,
    default_filter,
)


class TestLoadProxy(unittest.TestCase):
    def setUp(self):
        file = test_blend_file
        # file = r"D:\work\data\test_files\BlenderSS 2_82.blend"
        bpy.ops.wm.open_mainfile(filepath=file)
        self.proxy = BpyBlendProxy()
        self.proxy.load(default_context)

    def check(self, item, expected_elements):
        self.assertSetEqual(set(item._data.keys()), set(expected_elements))

    # @unittest.skip("")
    def test_blenddata(self):
        blend_data = self.proxy._data
        expected_data = {"scenes", "collections", "objects", "materials", "lights"}
        self.assertTrue(all([e in blend_data.keys() for e in expected_data]))

        self.check(self.proxy._data["scenes"], {"Scene_0", "Scene_1"})
        self.check(self.proxy._data["cameras"], {"Camera_0", "Camera_1"})
        self.check(self.proxy._data["objects"], {"Camera_obj_0", "Camera_obj_1", "Cone", "Cube", "Light"})
        self.check(
            self.proxy._data["collections"], {"Collection_0_0", "Collection_0_1", "Collection_0_0_0", "Collection_1_0"}
        )

    def test_blenddata_filtered(self):
        blend_data = self.proxy._data
        scene = blend_data["scenes"]._data["Scene_0"]._data
        self.assertTrue("eevee" in scene)

        filter_stack = copy.copy(default_filter)
        filter_stack.append({T.Scene: TypeFilterOut(T.SceneEEVEE)})
        proxy = BpyBlendProxy()
        proxy.load(Context(filter_stack))
        blend_data_ = proxy._data
        scene_ = blend_data_["scenes"]._data["Scene_0"]._data
        self.assertFalse("eevee" in scene_)

    # @unittest.skip("")
    def test_scene(self):
        scene = self.proxy._data["scenes"]._data["Scene_0"]._data
        # will vary slightly during tiune tuning of the default filter
        self.assertEqual(48, len(scene))

        objects = scene["objects"]._data
        self.assertEqual(4, len(objects))

        for o in objects.values():
            self.assertEqual(type(o), BpyIDRefProxy, o)

        # builtin attributes (floats)
        frame_properties = [name for name in scene.keys() if name.startswith("frame_")]
        self.assertEqual(9, len(frame_properties))

        # bpy_struct
        eevee = scene["eevee"]._data
        self.assertEqual(59, len(eevee))

        # PropertiesGroup
        cycles_proxy = scene["view_layers"]._data["View Layer"]._data["cycles"]
        self.assertIsInstance(cycles_proxy, BpyPropertyGroupProxy)
        self.assertEqual(32, len(cycles_proxy._data))

        # The master collection
        master_collection = scene["collection"]
        self.assertIsInstance(master_collection, BpyIDProxy)

    def test_collections(self):
        collections = self.proxy._data["collections"]
        coll_0_0 = collections._data["Collection_0_0"]._data

        coll_0_0_children = coll_0_0["children"]
        self.check(coll_0_0_children, {"Collection_0_0_0"})
        for c in coll_0_0_children._data.values():
            self.assertIsInstance(c, BpyIDRefProxy)

        coll_0_0_objects = coll_0_0["objects"]
        self.check(coll_0_0_objects, {"Camera_obj_0", "Camera_obj_1", "Cube", "Light"})
        for o in coll_0_0_objects._data.values():
            self.assertIsInstance(o, BpyIDRefProxy)

        pass


class TestProperties(unittest.TestCase):
    def test_one(self):
        context = default_context
        camera = D.cameras[0]
        props = dict(context.properties(camera))
        self.assertEqual(len(props), 39)
        self.assertIn("cycles", props.keys())
        item = D.cameras[0]
        props = context.properties(item)


class TestBlendData(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.open_mainfile(filepath=test_blend_file)

    def test_one(self):
        blenddata = BlendData.instance()
        scenes = blenddata.collection("scenes").bpy_collection()
        sounds = blenddata.collection("sounds").bpy_collection()
        # identity is not true
        self.assertEqual(scenes, D.scenes)
        self.assertEqual(sounds, D.sounds)
        self.assertIs(scenes["Scene_0"], D.scenes["Scene_0"])

    def test_derived_from_id(self):
        light = bpy.data.lights.new("new_area", "AREA")
        blenddata = BlendData.instance()
        collection_name = blenddata.bl_collection_name_from_ID(type(light))
        self.assertEqual(collection_name, "lights")


class TestAosSoa(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.open_mainfile(filepath=test_blend_file)

    def test_all_soa_grease_pencil(self):
        import array

        bpy.ops.object.gpencil_add(type="STROKE")
        proxy = BpyBlendProxy()
        proxy.load(default_context)
        gp_layers = proxy._data["grease_pencils"]._data["Stroke"]._data["layers"]
        gp_points = gp_layers._data["Lines"]._data["frames"]._data[0]._data["strokes"]._data[0]._data["points"]._data
        expected = (
            ("co", array.array, "f"),
            ("pressure", array.array, "f"),
            ("strength", array.array, "f"),
            ("uv_factor", array.array, "f"),
            ("uv_rotation", array.array, "f"),
            ("select", list, bool),
        )
        for name, type_, element_type in expected:
            self.assertIn("co", gp_points)
            item = gp_points[name]
            self.assertIsInstance(item, SoaElement)
            self.assertIsInstance(item._data, type_)
            if type_ is array.array:
                self.assertEqual(item._data.typecode, element_type)
            else:
                self.assertIsInstance(item._data[0], element_type)

        self.assertEqual(len(gp_points["pressure"]._data), len(gp_points["strength"]._data))
        self.assertEqual(3 * len(gp_points["pressure"]._data), len(gp_points["co"]._data))
