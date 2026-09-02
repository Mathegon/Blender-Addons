bl_info = {
    "name": "GLTF Multi Exporter",
    "author": "ChatGPT and Mattias Johansson",
    "version": (1, 8),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > GLTF Export",
    "description": "Export selected (or all visible) object(s) to GLB, GLB (Draco), and GLTF in separate folders",
    "category": "Import-Export",
}

import bpy
import os
import time
from mathutils import Vector

# ----------------------
# Properties
# ----------------------
class GLTFMultiExportSettings(bpy.types.PropertyGroup):
    export_path: bpy.props.StringProperty(name="Export Path", subtype='DIR_PATH', default="//RQP1")
    file_name: bpy.props.StringProperty(name="File Name", default="xxxx_PS01_S01_NV01_RQP1_4.0")

    use_blend_name: bpy.props.BoolProperty(
        name="Use .blend File Name",
        description="Use the current Blender file name for export",
        default=False
    )

    apply_modifiers: bpy.props.BoolProperty(name="Apply Modifiers", default=True)
    join_objects: bpy.props.BoolProperty(name="Join Selected Objects", default=False)
    join_visible: bpy.props.BoolProperty(
        name="Join Visible Objects",
        description=(
            "Export every visible mesh object in the scene, joined into one — "
            "ignores the current selection and overrides 'Join Selected Objects'. "
            "Non-mesh objects (lights, cameras, empties, etc.) are skipped."
        ),
        default=False
    )
    apply_pivot_transform: bpy.props.BoolProperty(
        name="Transform CCB",
        description="Set pivot to bottom center, move to origin, and apply rotation/scale",
        default=True
    )


# ----------------------
# Helpers
# ----------------------
def apply_all_modifiers(obj):
    bpy.context.view_layer.objects.active = obj
    for mod in obj.modifiers:
        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception:
            pass


def set_origin_bottom_center(obj):
    local_bbox = [Vector(corner) for corner in obj.bound_box]
    world_bbox = [obj.matrix_world @ v for v in local_bbox]

    min_z = min(v.z for v in world_bbox)
    avg_x = sum(v.x for v in world_bbox) / 8
    avg_y = sum(v.y for v in world_bbox) / 8

    bottom_center = Vector((avg_x, avg_y, min_z))

    bpy.context.scene.cursor.location = bottom_center
    bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')


def size_kb(path):
    return os.path.getsize(path) / 1024 if os.path.exists(path) else 0


def time_since(timestamp):
    if timestamp == 0:
        return "0s ago"

    delta = max(0, int(time.time() - timestamp))

    if delta < 60:
        return f"{delta}s ago"

    minutes = delta // 60
    return f"{minutes}m {delta % 60}s ago"


# ----------------------
# Operator
# ----------------------
class EXPORT_OT_gltf_multi(bpy.types.Operator):
    bl_idname = "export_scene.gltf_multi"
    bl_label = "Export Selected (GLTF Multi)"

    @classmethod
    def poll(cls, context):
        s = context.scene.gltf_multi_settings
        if s.join_visible:
            ok = any(
                obj.type == 'MESH' and obj.visible_get()
                for obj in context.view_layer.objects
            )
            if not ok and hasattr(cls, "poll_message_set"):
                cls.poll_message_set("No visible mesh objects to export")
            return ok
        else:
            ok = len(context.selected_objects) > 0
            if not ok and hasattr(cls, "poll_message_set"):
                cls.poll_message_set("No objects selected")
            return ok

    def execute(self, context):
        s = context.scene.gltf_multi_settings
        context.scene.gltf_export_success = False

        selected = context.selected_objects

        if s.join_visible:
            # Ignores selection entirely: gather every visible MESH object in
            # the current view layer. Restricted to meshes because
            # bpy.ops.object.join() only merges objects of the same type as
            # the active object — mixing in lights, cameras, empties, etc.
            # would error out or silently drop them from the join.
            export_source = [
                obj for obj in context.view_layer.objects
                if obj.visible_get() and obj.type == 'MESH'
            ]
            if not export_source:
                self.report({'ERROR'}, "No visible mesh objects to export")
                return {'CANCELLED'}
        else:
            export_source = selected
            if not export_source:
                self.report({'ERROR'}, "No objects selected")
                return {'CANCELLED'}

        base_path = bpy.path.abspath(s.export_path)
        paths = {
            "glb": os.path.join(base_path, "glb"),
            "glb_draco": os.path.join(base_path, "glb_draco"),
            "gltf": os.path.join(base_path, "gltf")
        }

        for p in paths.values():
            os.makedirs(p, exist_ok=True)

        # original_selection tracks what the user actually had selected
        # before running the operator, so it can be restored at the end —
        # this is independent of export_source, which may be the full set
        # of visible objects instead.
        original_selection = selected.copy()
        original_active = context.view_layer.objects.active

        bpy.ops.object.select_all(action='DESELECT')
        for obj in export_source:
            obj.select_set(True)
        bpy.ops.object.duplicate()

        working = context.selected_objects

        if s.apply_modifiers:
            for obj in working:
                apply_all_modifiers(obj)

        # "Join Visible Objects" implies joining regardless of the "Join
        # Selected Objects" toggle.
        if (s.join_visible or s.join_objects) and len(working) > 1:
            context.view_layer.objects.active = working[0]
            bpy.ops.object.join()
            # After joining, only the resulting single object remains selected.
            working = context.selected_objects

        if not working:
            self.report({'ERROR'}, "No objects to export")
            return {'CANCELLED'}

        export_obj = working[0]
        context.view_layer.objects.active = export_obj

        # Pivot transform + rename only make sense for a single resulting
        # object (either originally one object, or after joining). For an
        # un-joined multi-object export we skip both so we don't collapse
        # every object onto the world origin or clobber all their names.
        if len(working) == 1:
            if s.apply_pivot_transform:
                set_origin_bottom_center(export_obj)
                export_obj.location = (0, 0, 0)
                bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
            export_obj.name = "model"

        # Determine export name
        if s.use_blend_name and bpy.data.filepath:
            blend_name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
            name = blend_name
        else:
            name = s.file_name.strip() or export_obj.name

        # FIX: select every duplicated/working object before exporting, not
        # just the active one, so multi-object (non-joined) selections are
        # no longer silently dropped from the exported file.
        bpy.ops.object.select_all(action='DESELECT')
        for obj in working:
            obj.select_set(True)
        context.view_layer.objects.active = export_obj

        export_kwargs = {"use_selection": True, "export_apply": s.apply_modifiers}

        glb = os.path.join(paths["glb"], f"{name}.glb")
        draco = os.path.join(paths["glb_draco"], f"{name}.glb")
        gltf = os.path.join(paths["gltf"], f"{name}.gltf")

        bpy.ops.export_scene.gltf(filepath=glb, export_format='GLB', **export_kwargs)
        bpy.ops.export_scene.gltf(filepath=draco, export_format='GLB', export_draco_mesh_compression_enable=True, **export_kwargs)
        bpy.ops.export_scene.gltf(filepath=gltf, export_format='GLTF_SEPARATE', **export_kwargs)

        context.scene.gltf_size_glb = size_kb(glb)
        context.scene.gltf_size_draco = size_kb(draco)
        context.scene.gltf_size_gltf = size_kb(gltf)
        context.scene.gltf_file_count = 3
        context.scene.gltf_total_size = (
            context.scene.gltf_size_glb +
            context.scene.gltf_size_draco +
            context.scene.gltf_size_gltf
        )

        bpy.ops.object.delete()
        for obj in original_selection:
            obj.select_set(True)
        context.view_layer.objects.active = original_active

        context.scene.gltf_last_export_time = time.time()
        context.scene.gltf_export_success = True

        # Force immediate UI refresh
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

        # Reset export button after 10 seconds.
        # FIX: don't close over the operator's `context` argument here — by
        # the time this timer fires, that context may no longer be valid.
        # Use bpy.context (the live, current context) instead.
        def reset_button():
            bpy.context.scene.gltf_export_success = False
            return None

        bpy.app.timers.register(reset_button, first_interval=10.0)

        self.report({'INFO'}, "Export complete")
        return {'FINISHED'}


# ----------------------
# Live UI Timer
# ----------------------
def ui_timer():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'UI':
                        region.tag_redraw()
    return 1.0  # refresh every second


# ----------------------
# Options Popover
# ----------------------
class EXPORT_PT_gltf_multi_options(bpy.types.Panel):
    bl_label = "Export Options"
    bl_idname = "EXPORT_PT_gltf_multi_options"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    # No bl_category: this keeps the panel from showing up as its own tab —
    # it's only ever drawn as a popover via layout.popover() below.

    def draw(self, context):
        s = context.scene.gltf_multi_settings
        layout = self.layout

        layout.prop(s, "apply_modifiers")

        row = layout.row()
        row.enabled = not s.join_visible
        row.prop(s, "join_objects")
        layout.prop(s, "join_visible")

        layout.prop(s, "apply_pivot_transform")


# ----------------------
# Panel
# ----------------------
class EXPORT_PT_gltf_multi_panel(bpy.types.Panel):
    bl_label = "GLTF Multi Export"
    bl_idname = "EXPORT_PT_gltf_multi_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GLTF Export"

    def draw(self, context):
        layout = self.layout
        s = context.scene.gltf_multi_settings

        layout.prop(s, "export_path")
        layout.prop(s, "file_name")
        layout.prop(s, "use_blend_name")

        row = layout.row(align=True)
        row.popover(panel="EXPORT_PT_gltf_multi_options", text="", icon='PREFERENCES')
        row.label(text="Export Options")

        layout.separator()

        # Export button state
        if context.scene.gltf_export_success:
            layout.operator("export_scene.gltf_multi", text="Export Complete", icon='CHECKMARK', depress=True)
        else:
            label = "Export Visible" if s.join_visible else "Export Selected"
            layout.operator("export_scene.gltf_multi", text=label, icon='EXPORT')

        # Persisted stats (shown after first export)
        if context.scene.gltf_last_export_time > 0:
            layout.separator()
            layout.label(text=f"✔ Exported {context.scene.gltf_file_count} files ({context.scene.gltf_total_size:.0f} KB)")

            def size_row(label, size):
                row = layout.row()
                if size > 1024:
                    row.alert = True
                row.label(text=f"{label}: {size:.0f} KB")

            size_row("GLB", context.scene.gltf_size_glb)
            size_row("GLB (Draco)", context.scene.gltf_size_draco)
            size_row("GLTF", context.scene.gltf_size_gltf)

            path = bpy.path.abspath(s.export_path)
            last_time = time_since(context.scene.gltf_last_export_time)
            op = layout.operator("wm.path_open", text=f"Open Export Folder ({last_time})", icon='FILE_FOLDER')
            op.filepath = path


# ----------------------
# Registration
# ----------------------
classes = (
    GLTFMultiExportSettings,
    EXPORT_OT_gltf_multi,
    EXPORT_PT_gltf_multi_options,
    EXPORT_PT_gltf_multi_panel,
)


def register():
    bpy.app.timers.register(ui_timer)

    # FIX: removed the duplicate registration of gltf_export_success below.
    bpy.types.Scene.gltf_export_success = bpy.props.BoolProperty(default=False)
    bpy.types.Scene.gltf_file_count = bpy.props.IntProperty(default=0)
    bpy.types.Scene.gltf_total_size = bpy.props.FloatProperty(default=0.0)
    bpy.types.Scene.gltf_size_glb = bpy.props.FloatProperty(default=0.0)
    bpy.types.Scene.gltf_size_draco = bpy.props.FloatProperty(default=0.0)
    bpy.types.Scene.gltf_size_gltf = bpy.props.FloatProperty(default=0.0)
    bpy.types.Scene.gltf_last_export_time = bpy.props.FloatProperty(default=0.0)

    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.gltf_multi_settings = bpy.props.PointerProperty(type=GLTFMultiExportSettings)


def unregister():
    # FIX: stop the per-second UI redraw timer when the add-on is disabled,
    # otherwise it keeps running (and referencing bpy.context) indefinitely.
    if bpy.app.timers.is_registered(ui_timer):
        bpy.app.timers.unregister(ui_timer)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.gltf_multi_settings
    del bpy.types.Scene.gltf_export_success
    del bpy.types.Scene.gltf_file_count
    del bpy.types.Scene.gltf_total_size
    del bpy.types.Scene.gltf_size_glb
    del bpy.types.Scene.gltf_size_draco
    del bpy.types.Scene.gltf_size_gltf
    del bpy.types.Scene.gltf_last_export_time


if __name__ == "__main__":
    register()
