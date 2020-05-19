import array
from enum import IntEnum
import logging
from typing import Any, Mapping, Union, Set, List, Tuple
from uuid import uuid4
from contextlib import contextmanager

import bpy
import bpy.types as T  # noqa
import mathutils

from mixer.blender_data.filter import Context
from mixer.blender_data.blenddata import (
    BlendData,
    collection_name_to_type,
    rna_identifier_to_collection_name,
    bl_rna_to_type,
)
from mixer.blender_data.types import is_builtin, is_vector, is_matrix

logger = logging.Logger(__name__, logging.INFO)


def debug_check_stack_overflow(func, *args, **kwargs):
    """
    Use as a function decorator to detect probable stack overflow in case of circular references

    Beware : inspect performance is very poor.
    sys.setrecursionlimit cannot be used because it will possibly break the Blender VScode
    plugin and StackOverflowException is not caught by VScode "Raised exceptions" breakpoint.
    """

    def wrapper(*args, **kwargs):
        import inspect

        if len(inspect.stack(0)) > 50:
            raise RuntimeError("Possible stackoverflow")
        return func(*args, **kwargs)

    return wrapper


class BlendDataVisitContext:
    """
    Context class only used during BpyBlendProxy construction, to keep contextual data during traversal
    of the blender data hierarchy and perform safety checkes
    """

    # ID elements stored in bpy.data.* collections, computed before recursive visit starts:
    root_ids: Set[bpy.types.ID] = set()
    serialized_addresses: Set[bpy.types.ID] = set()  # Already serialized addresses (struct or IDs), for debug
    property_stack: List[Tuple[str, any]] = []  # Stack of properties up to this point in the visit

    def __init__(self, context: Context):
        # Iterate over IDs at the root level of blender file to serialize them as IDref deeper in the hierarchy
        for name, _ in context.properties(bpy_type=T.BlendData):
            if name in collection_name_to_type:
                bl_collection = getattr(bpy.data, name)
                for _id_name, item in bl_collection.items():
                    ensure_uuid(item)
                    self.root_ids.add(item)

    @contextmanager
    def enter(self, property_name, property_value):
        self.property_stack.append((property_name, property_value))
        yield
        self.property_stack.pop()
        self.serialized_addresses.add(id(property_value))

    def visit_depth(self):
        # Utility for debug
        return len(self.property_stack)

    def property_fullpath(self):
        # Utility for debug
        return ".".join([p[0] for p in self.property_stack])


class LoadElementAs(IntEnum):
    STRUCT = 0
    ID_REF = 1
    ID_DEF = 2


def same_rna(a, b):
    return a.bl_rna == b.bl_rna


def is_ID_subclass_rna(bl_rna):  # noqa
    """
    Return true if the RNA is of a subclass of bpy.types.ID
    """
    return issubclass(bl_rna_to_type(bl_rna), bpy.types.ID)


def load_as_what(attr_property: bpy.types.Property, attr: any, visit_context: BlendDataVisitContext):
    """
    Determine if we must load an attribute as a struct, a blenddata collection element (ID_DEF)
    or a reference to a BlendData collection element (ID_REF)

    All T.Struct are loaded as struct
    All T.ID are loaded ad IDRef (that is pointer into a D.blendata collection) except
    for specific case. For instance the scene master "collection" is not a D.collections item.

    Arguments
    parent -- the type that contains the attribute names attr_name, for instance T.Scene
    attr_property -- a bl_rna property of a attribute, that can be a CollectionProperty or a "plain" attribute
    """
    # Reference to an ID element at the root of the blend file
    if attr in visit_context.root_ids:
        return LoadElementAs.ID_REF

    if same_rna(attr_property, T.CollectionProperty):
        if is_ID_subclass_rna(attr_property.fixed_type.bl_rna):
            # Collections at root level are not handled by this code (See BpyBlendProxy.load()) so here it
            # should be a nested collection and IDs should be ref to root collections.
            return LoadElementAs.ID_REF
        else:
            # Only collections at the root level of blendfile are id def, so here it can only be struct
            return LoadElementAs.STRUCT

    if same_rna(attr_property, T.PointerProperty):
        element_property = attr_property.fixed_type
    else:
        element_property = attr_property

    if is_ID_subclass_rna(element_property.bl_rna):
        return LoadElementAs.ID_DEF

    return LoadElementAs.STRUCT


# @debug_check_stack_overflow
def read_attribute(attr: any, attr_property: any, context: Context, visit_context: BlendDataVisitContext):
    """
    Load a property into a python object of the appropriate type, be it a Proxy or a native python object


    """
    with visit_context.enter(attr_property.identifier, attr):
        attr_type = type(attr)

        if is_builtin(attr_type):
            return attr
        if is_vector(attr_type):
            return list(attr)
        if is_matrix(attr_type):
            return [list(col) for col in attr.col]

        # We have tested the types that are usefully reported by the python binding, now harder work.
        # These were implemented first and may be better implemented with the bl_rna property of the parent struct
        if attr_type == T.bpy_prop_array:
            return [e for e in attr]

        if attr_type == T.bpy_prop_collection:
            # need to know for each element if it is a ref or id
            load_as = load_as_what(attr_property, attr, visit_context)
            if load_as == LoadElementAs.STRUCT:
                return BpyPropStructCollectionProxy().load(attr, context, visit_context)
            elif load_as == LoadElementAs.ID_REF:
                # References into Blenddata collection, for instance D.scenes[0].objects
                return BpyPropDataCollectionProxy().load_as_IDref(attr, visit_context)
            elif load_as == LoadElementAs.ID_DEF:
                # is  BlendData collection, for instance D.objects
                return BpyPropDataCollectionProxy().load_as_ID(attr, context, visit_context)

        # TODO merge with previous case
        if isinstance(attr_property, T.CollectionProperty):
            return BpyPropStructCollectionProxy().load(attr, context, visit_context)

        bl_rna = attr_property.bl_rna
        if bl_rna is None:
            logger.warning("Unimplemented attribute %s", attr)
            return None

        assert issubclass(attr_type, T.PropertyGroup) == issubclass(attr_type, T.PropertyGroup)
        if issubclass(attr_type, T.PropertyGroup):
            return BpyPropertyGroupProxy().load(attr, context, visit_context)

        load_as = load_as_what(attr_property, attr, visit_context)
        if load_as == LoadElementAs.STRUCT:
            return BpyStructProxy().load(attr, context, visit_context)
        elif load_as == LoadElementAs.ID_REF:
            return BpyIDRefProxy().load(attr, visit_context)
        elif load_as == LoadElementAs.ID_DEF:
            return BpyIDProxy().load(attr, context, visit_context)

        # assert issubclass(attr_type, T.bpy_struct) == issubclass(attr_type, T.bpy_struct)
        raise AssertionError("unexpected code path")
        # should be handled above
        if issubclass(attr_type, T.bpy_struct):
            return BpyStructProxy().load(attr, visit_context)

        raise ValueError(f"Unsupported attribute type {attr_type} without bl_rna for attribute {attr} ")


class Proxy:
    def __eq__(self, other):
        if self.__class__ is not other.__class__:
            return False
        if len(self._data) != len(other._data):
            return False

        for k, v in self._data.items():
            if k not in other._data.keys():
                return False
            if v != other._data[k]:
                return False
        return True

    def save(self, bl_instance: any, _):
        """
        Save this proxy into a blender object
        """
        logging.warning(f"save : Not implemented {self.__class__} {bl_instance}")


class StructLikeProxy(Proxy):
    """
    Holds a copy of a Blender bpy_struct
    """

    # TODO limit depth like in multiuser. Anyhow, there are circular references in f-curves
    def __init__(self):

        # We care for some non readonly properties. Collection object are tagged read_only byt can be updated with

        # Beware :

        # >>> bpy.types.Scene.bl_rna.properties['collection']
        # <bpy_struct, PointerProperty("collection")>

        # TODO is_readonly may be only interesting for "base types". FOr Collections it seems always set to true
        # meaning that the collection property slot cannot be updated although the object is mutable
        # TODO we also care for some readonly properties that are in fact links to data collections

        # The property information are taken from the containing class, not from the attribute.
        # So we get :
        #   T.Scene.bl_rna.properties['collection']
        #       <bpy_struct, PointerProperty("collection")>
        #   T.Scene.bl_rna.properties['collection'].fixed_type
        #       <bpy_struct, Struct("Collection")>
        # But if we take the information in the attribute we get information for the dereferenced
        # data
        #   D.scenes[0].collection.bl_rna
        #       <bpy_struct, Struct("Collection")>
        #
        # We need the former to make a difference between T.Scene.collection and T.Collection.children.
        # the former is a pointer
        self._data = {}
        pass

    def load(self, bl_instance: any, context: Context, visit_context: BlendDataVisitContext):

        """
        Load a Blender object into this proxy
        """
        self._data.clear()
        for name, bl_rna_property in context.properties(bl_instance):
            attr = getattr(bl_instance, name)
            attr_value = read_attribute(attr, bl_rna_property, context, visit_context)
            if attr_value is not None:
                self._data[name] = attr_value
        return self

    def save(self, bl_instance: any, key: Union[int, str]):
        """
        Load a Blender object into this proxy
        """
        assert isinstance(key, int) or isinstance(key, str)
        if isinstance(key, int):
            target = bl_instance[key]
        elif isinstance(bl_instance, T.bpy_prop_collection):
            # TODO append an element :
            # https://blenderartists.org/t/how-delete-a-bpy-prop-collection-element/642185/4
            target = bl_instance.get(key)
        else:
            target = getattr(bl_instance, key)

        if target is None:
            logging.warning(f"Cannot write to '{bl_instance}', attribute '{key}' because it does not exist")
            return

        for k, v in self._data.items():
            write_attribute(target, k, v)


class BpyPropertyGroupProxy(StructLikeProxy):
    pass


class BpyStructProxy(StructLikeProxy):
    pass


class BpyIDProxy(BpyStructProxy):
    """
    Holds a copy of a Blender ID, i.e a type stored in bpy.data, like Object and Material
    """

    def __init__(self):
        super().__init__()

    def load(self, bl_instance, context: Context, visit_context: BlendDataVisitContext):
        # TODO check that bl_instance class derives from ID
        super().load(bl_instance, context, visit_context)
        self.mixer_uuid = bl_instance.mixer_uuid
        return self

    def target(self, bl_instance: any, attr_name: str):
        if isinstance(bl_instance, bpy.types.bpy_prop_collection):
            return bl_instance[attr_name]
        else:
            return getattr(bl_instance, attr_name)

    def pre_save(self, bl_instance: any, attr_name: str):
        """
        Process attributes that must be save first and return a possibily updated reference to tha target
        """
        target = self.target(bl_instance, attr_name)

        if isinstance(target, bpy.types.Scene):
            # Set 'use_node' to True first is the only way I know to be able to set the 'node_tree' attribute
            use_nodes = self._data.get("use_nodes")
            if use_nodes:
                write_attribute(target, "use_nodes", True)
            sequence_editor = self._data.get("sequence_editor")
            if sequence_editor is not None and target.sequence_editor is None:
                target.sequence_editor_create()
        elif isinstance(target, bpy.types.Light):
            # required first to have access to new light type attributes
            light_type = self._data.get("type")
            if light_type is not None and light_type != target.type:
                write_attribute(target, "type", light_type)
                # must reload the reference
                target = self.target(bl_instance, attr_name)
        return target

    def save(self, bl_instance: any, attr_name: str):
        """
        - bl_instance: the container attribute
        """
        target = self.pre_save(bl_instance, attr_name)
        for k, v in self._data.items():
            write_attribute(target, k, v)


class BpyIDRefProxy(Proxy):
    """
    A reference to an item of bpy_prop_collection in bpy.data member
    """

    def __init__(self):
        pass

    def load(self, bl_instance, visit_context: BlendDataVisitContext):
        # Nothing to filter here, so we do not need the context/filter

        # Walk up to child of ID
        class_bl_rna = bl_instance.bl_rna
        while class_bl_rna.base is not None and class_bl_rna.base != bpy.types.ID.bl_rna:
            class_bl_rna = class_bl_rna.base

        # Safety check that the instance can really be accessed from root collections with its full name
        assert getattr(bpy.data, rna_identifier_to_collection_name[class_bl_rna.identifier], None) is not None
        assert bl_instance.name_full in getattr(bpy.data, rna_identifier_to_collection_name[class_bl_rna.identifier])

        # TODO for easier access could keep a ref to the BpyBlendProxy
        # TODO maybe this information does not belong to _data and _data should be reserved to "fields"
        self._data = (
            # Blenddata collection name, e.g. 'objects', 'lights'
            BlendData.instance().bl_collection_name_from_inner_identifier(class_bl_rna.identifier),
            # key in blenddata collection
            bl_instance.name_full,
        )
        return self

    def save(self, bl_instance: any, attr_name: str):
        """
        Save this proxy into bl_instance.attr_name
        """

        # Cannot save into an attribute, which looks like an r-value.
        # When setting my_scene.world wen must setattr(scene, "world", data) and cannot
        # assign scene.world
        collection_name = self._data[0]
        collection_key = self._data[1]

        # TODO the identifier garget doest not have the same semantics everywhere
        # here pointee somewhere else lvalue
        target = getattr(bpy.data, collection_name)[collection_key]
        if isinstance(bl_instance, T.bpy_prop_collection):
            logging.warning(f"not implemented IDRef into collection {attr_name} for {bl_instance}...")
        else:
            if not bl_instance.bl_rna.properties[attr_name].is_readonly:
                try:
                    setattr(bl_instance, attr_name, target)
                except Exception as e:
                    logging.warning(f"write attribute skipped {attr_name} for {bl_instance}...")
                    logging.warning(f" ...Error: {repr(e)}")


def ensure_uuid(item: bpy.types.ID):
    if item.get("mixer_uuid") is None:
        item.mixer_uuid = str(uuid4())


# in sync with soa_initializers
soable_properties = {
    T.BoolProperty,
    T.IntProperty,
    T.FloatProperty,
    mathutils.Vector,
    mathutils.Color,
    mathutils.Quaternion,
}

# in sync with soa_initializers
soa_initializers = {
    bool: [False],
    int: array.array("l", [0]),
    float: array.array("f", [0.0]),
    mathutils.Vector: array.array("f", [0.0]),
    mathutils.Color: array.array("f", [0.0]),
    mathutils.Quaternion: array.array("f", [0.0]),
}

# TODO : is there any way to find these automatically ? Seems easy to determine if a struct is simple enough so that
# an array of struct can be loaded as an Soa, but how to get the element type of a bpy _prop_collection ?
# MeshVertex must be handled as SOA although "groups" is a variable length item. Enums are not handled by foreach_get()
soable_collections = {
    T.GPencilStrokePoints.bl_rna,
    T.MeshVertices.bl_rna,
    # many more Mesh elements
}


def soable_collection(bl_collection):
    return hasattr(bl_collection, "bl_rna") and bl_collection.bl_rna in soable_collections


def soa_initializer(attr_type, length):
    # According to bpy_rna.c:foreach_getset() and rna_access.c:rna_raw_access() implementations,
    # some cases are implemented as memcpy (buffer interface) or array iteration (sequences),
    # with more subcases that require reallocation when the buffer type is not suitable,
    # so try to be smart
    element_init = soa_initializers[attr_type]
    if isinstance(element_init, array.array):
        return array.array(element_init.typecode, element_init.tolist() * length)
    elif isinstance(element_init, list):
        return element_init * length


class AosElement(Proxy):
    """
    A structure member inside a bpy_prop_collection aloded as a structure orf array element

    For instance, MeshVertex.groups is a bpy_prop_collection of variable size and it cannot
    be loaded as an Soa in Mesh.vertices. So Mesh.vertices loads a "groups" AosElement
    """

    def __init__(self):
        self._data: Union[array.array, [List]] = None

    def load(
        self, bl_collection: bpy.types.bpy_prop_collection, attr_name: str,
    ):
        # the struct element may be missing is soable_properties
        logging.warning(f"Not implemented : property {bl_collection}.{attr_name}")
        return self


class SoaElement(Proxy):
    """
    A structure member inside a bpy_prop_collection loaded as a structure of array element

    For instance, Mesh.vertices[].co is loaded as an SoaElement of Mesh.vertices. Its _data is an array
    """

    def __init__(self):
        self._data: Union[array.array, [List]] = None

    def load(self, bl_collection: bpy.types.bpy_prop_collection, attr_name: str, prototype_item):
        # TODO do not reallocate if size does not change
        attr = getattr(prototype_item, attr_name)
        attr_type = type(attr)
        length = len(bl_collection)
        if is_vector(attr_type):
            length *= len(attr)
        buffer = soa_initializer(attr_type, length)
        bl_collection.foreach_get(attr_name, buffer)
        self._data = buffer
        return self


class BpyPropStructCollectionProxy(Proxy):
    """
    Proxy to a bpy_prop_collection of non-ID in bpy.data
    """

    def __init__(self):
        self._data: Mapping[Union[str, int], BpyIDProxy] = {}

    def load(
        self, bl_collection: bpy.types.bpy_prop_collection, context: Context, visit_context: BlendDataVisitContext
    ):

        if len(bl_collection) == 0:
            return self

        # no keys : it is a sequence. However bl_collection.items() returns [(index, item)...]
        is_sequence = not bl_collection.keys()

        if is_sequence:
            if soable_collection(bl_collection):
                # TODO too much work at load time to find soable information. Do it once for all.

                # Hybrid array_of_struct/ struct_of_array
                # Hybrid because MeshVertex.groups does not have a fixed size and is not soa-able, but we want
                # to treat other MeshVertex members as SOAs.
                # Could be made more efficient later on. Keep the code simple until save() is implemented
                # and we need better
                prototype_item = bl_collection[0]
                for attr_name, bl_rna_property in context.properties(prototype_item):
                    is_soable = any(isinstance(bl_rna_property, soable) for soable in soable_properties)
                    if is_soable:
                        self._data[attr_name] = SoaElement().load(bl_collection, attr_name, prototype_item)
                    else:
                        self._data[attr_name] = AosElement().load(bl_collection, attr_name)
            else:
                # WIP and probably wrong
                # array of struct
                
                self._data = [BpyStructProxy().load(v, context, visit_context) for v in bl_collection.values()]

                # must probably be something like
                # self._data = [read_attribute(v, context, visit_context) for v in bl_collection.values()]
        else:
            # WIP and probably wrong
            # dict of struct
            # is this case possible or is it just for blenddata collections
            self._data = {k: BpyStructProxy().load(v, context, visit_context) for k, v in bl_collection.items()}

            # must probably be something like
            # self._data = {k: read_attribute(v, context, visit_context) for k, v in bl_collection.items()}

        return self

    def save(self, bl_instance: any, attr_name: str):
        """
        Load a Blender object into this proxy
        """
        target = getattr(bl_instance, attr_name)
        for k, v in self._data.items():
            write_attribute(target, k, v)


# TODO derive from BpyIDProxy
class BpyPropDataCollectionProxy(Proxy):
    """
    Proxy to a bpy_prop_collection of ID in bpy.data. May not work as is for bpy_prop_collection on non-ID
    """

    def __init__(self):
        self._data: Mapping[str, BpyIDProxy] = {}

    def __len__(self):
        return len(self._data)

    def load_as_ID(  # noqa N802
        self, bl_collection: bpy.types.bpy_prop_collection, context: Context, visit_context: BlendDataVisitContext
    ):
        """
        Load bl_collection elements as plain IDs, with all element properties. Use this lo load from bpy.data
        """
        for name, item in bl_collection.items():
            with visit_context.enter(name, item):
                ensure_uuid(item)
                self._data[name] = BpyIDProxy().load(item, context, visit_context)

        return self

    def load_as_IDref(  # noqa N802
        self, bl_collection: bpy.types.bpy_prop_collection, visit_context: BlendDataVisitContext
    ):
        """
        Load bl_collection elements as referenced into bpy.data
        """
        for name, item in bl_collection.items():
            with visit_context.enter(name, item):
                self._data[name] = BpyIDRefProxy().load(item, visit_context)
        return self

    def save(self, bl_instance: any, attr_name: str):
        """
        Load a Blender object into this proxy
        """
        target = getattr(bl_instance, attr_name)
        for k, v in self._data.items():
            write_attribute(target, k, v)

    def find(self, key: str):
        return self._data[key]

    def update(self, diff):
        # TODO with context
        """
        Update the proxy according to the diff
        """
        visit_context = BlendDataVisitContext()
        for name, bl_collection in diff.items_added.items():
            item = bl_collection[name]
            self._data[name] = BpyIDProxy().load(item, visit_context)
        for name in diff.items_removed:
            del self._data[name]
        for old_name, new_name in diff.items_renamed:
            self._data[new_name] = self._data[old_name]
            del self._data[old_name]
        for name, delta in diff.items_updated:
            self._data[name].update(delta)


class BpyBlendProxy(Proxy):
    def __init__(self, *args, **kwargs):
        self._data: Mapping[str, BpyPropDataCollectionProxy] = {}

    def get_non_empty_collections(self):
        return {key: value for key, value in self._data.items() if len(value) > 0}

    def load(self, context: Context):
        visit_context = BlendDataVisitContext(context)

        for name, _ in context.properties(bpy_type=T.BlendData):
            collection = getattr(bpy.data, name)
            with visit_context.enter(name, collection):
                self._data[name] = BpyPropDataCollectionProxy().load_as_ID(collection, context, visit_context)
        return self

    def find(self, collection_name: str, key: str) -> BpyIDProxy:
        if not self._data:
            return None
        collection_proxy = self._data.get(collection_name)
        if collection_proxy is None:
            return None
        return collection_proxy.find(key)

    def update(self, diff):
        for name in self.iter_all():
            deltas = diff.deltas.get(name)
            if deltas is not None:
                self._data[name].update(diff.deltas[name])

    def clear(self):
        self._data.clear()


proxy_classes = [
    BpyIDProxy,
    BpyIDRefProxy,
    BpyStructProxy,
    BpyPropertyGroupProxy,
    BpyPropStructCollectionProxy,
    BpyPropDataCollectionProxy,
]


def write_attribute(bl_instance, key: Union[str, int], value: Any):
    """
    Load a property into a python object of the appropriate type, be it a Proxy or a native python object
    """
    type_ = type(value)
    if type_ not in proxy_classes:
        # TEMP we should not have readonly items
        assert type(key) is str
        if not bl_instance.bl_rna.properties[key].is_readonly:
            try:
                setattr(bl_instance, key, value)
            except Exception as e:
                logging.warning(f"write attribute skipped {key} for {bl_instance}...")
                logging.warning(f" ...Error: {repr(e)}")
        return
    else:
        value.save(bl_instance, key)

        # if type(key) is int:
        #     # Collection with int key (vertices, points, ...)
        #     if len(bl_instance):
        #         attr = bl_instance[key]
        #     else:
        #         logging.warning(f"write attribute skipped {key} for {bl_instance} - not implemented (array insertion)")
        # else:
        #     # Collection with a string key (T.BlendataObjects, T.ViewLayers)
        #     # or a mapping (T.bpy_struct)
        #     value.save(bl_instance, key)
        return
    raise NotImplementedError
