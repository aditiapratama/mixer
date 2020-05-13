"""
Functions to be remotely executed in Blender via python_server.py

Remote execution relies on source code extractiona and transmission to the
execution sever, so each function must contain its imports
"""


def connect():
    import bpy

    bpy.ops.mixer.connect()


def disconnect():
    import bpy

    bpy.ops.mixer.disconnect()


def create_room():
    import bpy

    bpy.ops.mixer.create_room()


def set_log_level(log_level):
    import mixer

    mixer.data.set_log_level(None, log_level)


def join_room(room_name: str = "mixer_unittest"):
    import mixer

    mixer.operators.join_room(room_name)