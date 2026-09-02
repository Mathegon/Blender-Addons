bl_info = {
    "name": "UV Rescale Zoom Helper",
    "author": "Mattias + Copilot",
    "version": (1, 1),
    "blender": (3, 0, 0),
    "location": "UV Editor > Sidebar > View Tab",
    "description": (
        "Works around Blender's fixed UV/Image editor zoom-out limit by temporarily "
        "rescaling UV coordinates so a huge or real-world-scale layout (e.g. imported "
        "from 3ds Max) fits inside the editor's normal, unclamped zoom range. The "
        "shrink factor is calculated automatically from the UV bounds."
    ),
    "category": "UV",
}

import bpy
import bmesh
from mathutils import Vector

# After shrinking, aim for the largest UV dimension to be about this many units.
# Verified empirically: layouts around this size sit comfortably inside the
# UV/Image editor's normal (unclamped) zoom range.
TARGET_SPAN = 1.0


def get_uv_bounds(bm, uv_layer):
    """Bounding box (min, max) of every UV coordinate on this layer."""
    xs, ys = [], []
    for face in bm.faces:
        for loop in face.loops:
            uv = loop[uv_layer].uv
            xs.append(uv.x)
            ys.append(uv.y)
    if not xs:
        return None
    return Vector((min(xs), min(ys))), Vector((max(xs), max(ys)))


def compute_auto_factor(bmin, bmax, target_span=TARGET_SPAN):
    """Suggest a shrink factor that brings the largest UV dimension down to target_span."""
    span_x = bmax.x - bmin.x
    span_y = bmax.y - bmin.y
    max_span = max(span_x, span_y)
    if max_span <= 0:
        return None, (span_x, span_y)
    factor = max(max_span / target_span, 1.0)
    return factor, (span_x, span_y)


class UV_OT_RescaleZoomEstimate(bpy.types.Operator):
    """Measure the active UV layout's bounds and preview the shrink factor that would be used, without changing anything"""
    bl_idname = "uv.rescale_zoom_estimate"
    bl_label = "Estimate Shrink Factor"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.edit_object
        return obj is not None and obj.type == 'MESH' and obj.data.uv_layers.active is not None

    def execute(self, context):
        obj = context.edit_object
        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        uv_layer = bm.loops.layers.uv.active
        bounds = get_uv_bounds(bm, uv_layer)
        if bounds is None:
            self.report({'ERROR'}, "No UV data found on this mesh")
            return {'CANCELLED'}
        bmin, bmax = bounds
        factor, (span_x, span_y) = compute_auto_factor(bmin, bmax)

        wm = context.window_manager
        wm.uv_zoom_helper_estimate_for = mesh.name
        wm.uv_zoom_helper_estimate_span_x = span_x
        wm.uv_zoom_helper_estimate_span_y = span_y
        wm.uv_zoom_helper_estimate_factor = factor if factor else 1.0

        self.report({'INFO'}, f"UV span: {span_x:.4g} x {span_y:.4g}  ->  suggested factor: {factor:.4g}x")
        return {'FINISHED'}


class UV_OT_RescaleZoomShrink(bpy.types.Operator):
    """Shrink the active UV layout around its center so it fits inside the editor's normal zoom range"""
    bl_idname = "uv.rescale_zoom_shrink"
    bl_label = "Shrink UVs"
    bl_options = {'REGISTER', 'UNDO'}

    auto_factor: bpy.props.BoolProperty(
        name="Auto Factor",
        description="Calculate the shrink factor automatically from the current UV bounds",
        default=True,
    )
    factor: bpy.props.FloatProperty(
        name="Shrink Factor",
        description="Divide UV coordinates by this factor. Auto-calculated from the UV bounds unless 'Auto Factor' is disabled",
        default=1.0,
        min=1.0001,
        soft_max=1000000.0,
    )

    @classmethod
    def poll(cls, context):
        obj = context.edit_object
        return obj is not None and obj.type == 'MESH' and obj.data.uv_layers.active is not None

    def execute(self, context):
        obj = context.edit_object
        mesh = obj.data

        if mesh.get("uv_zoom_helper_active"):
            self.report({'WARNING'}, "UVs are already shrunk — restore them before shrinking again")
            return {'CANCELLED'}

        bm = bmesh.from_edit_mesh(mesh)
        uv_layer = bm.loops.layers.uv.active

        bounds = get_uv_bounds(bm, uv_layer)
        if bounds is None:
            self.report({'ERROR'}, "No UV data found on this mesh")
            return {'CANCELLED'}

        bmin, bmax = bounds
        pivot = (bmin + bmax) / 2

        if self.auto_factor:
            auto, (span_x, span_y) = compute_auto_factor(bmin, bmax)
            if auto is None:
                self.report({'ERROR'}, "UVs have zero area — nothing to shrink")
                return {'CANCELLED'}
            if auto <= 1.0 + 1e-6:
                self.report(
                    {'INFO'},
                    f"UV span is already {span_x:.3g} x {span_y:.3g} — fits the normal zoom range, no shrink needed",
                )
                return {'CANCELLED'}
            self.factor = auto

        for face in bm.faces:
            for loop in face.loops:
                uv = loop[uv_layer]
                # Recenter at the origin (not at the pivot's own coordinates) before
                # dividing. Float precision is relative to magnitude, so for
                # real-world-scale UVs (thousands of units from 3ds Max etc.) keeping
                # values near the pivot's original huge magnitude would throw away the
                # precision this shrink is supposed to buy back -- and that lost
                # precision gets amplified right back out by `factor` on restore.
                uv.uv = (uv.uv - pivot) / self.factor

        bmesh.update_edit_mesh(mesh)

        # remember exactly how to undo this, even across a save/reload
        mesh["uv_zoom_helper_active"] = True
        mesh["uv_zoom_helper_factor"] = self.factor
        mesh["uv_zoom_helper_pivot"] = (pivot.x, pivot.y)

        self.report({'INFO'}, f"Shrunk UVs {self.factor:.4g}x around ({pivot.x:.3f}, {pivot.y:.3f})")
        return {'FINISHED'}


class UV_OT_RescaleZoomRestore(bpy.types.Operator):
    """Restore UVs to their original scale after shrinking"""
    bl_idname = "uv.rescale_zoom_restore"
    bl_label = "Restore UVs"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.edit_object
        return (
            obj is not None
            and obj.type == 'MESH'
            and obj.data.uv_layers.active is not None
            and obj.data.get("uv_zoom_helper_active")
        )

    def execute(self, context):
        obj = context.edit_object
        mesh = obj.data

        factor = mesh["uv_zoom_helper_factor"]
        pivot = Vector(mesh["uv_zoom_helper_pivot"])

        bm = bmesh.from_edit_mesh(mesh)
        uv_layer = bm.loops.layers.uv.active

        for face in bm.faces:
            for loop in face.loops:
                uv = loop[uv_layer]
                uv.uv = uv.uv * factor + pivot

        bmesh.update_edit_mesh(mesh)

        del mesh["uv_zoom_helper_active"]
        del mesh["uv_zoom_helper_factor"]
        del mesh["uv_zoom_helper_pivot"]

        self.report({'INFO'}, f"Restored UVs (undid {factor:.4g}x shrink)")
        return {'FINISHED'}


class UV_PT_RescaleZoomPanel(bpy.types.Panel):
    """Creates a panel in the UV Editor sidebar"""
    bl_label = "Rescale Zoom Helper"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "View"

    def draw(self, context):
        layout = self.layout
        obj = context.edit_object
        mesh = obj.data if (obj and obj.type == 'MESH') else None
        active = mesh is not None and mesh.get("uv_zoom_helper_active")

        if not obj or obj.type != 'MESH':
            layout.label(text="Enter Edit Mode on a mesh", icon='INFO')
            return

        if active:
            layout.label(text=f"Shrunk {mesh['uv_zoom_helper_factor']:.4g}x", icon='INFO')
            layout.operator("uv.rescale_zoom_restore", icon='LOOP_BACK')
            return

        col = layout.column()
        col.operator("uv.rescale_zoom_estimate", icon='VIEWZOOM', text="Estimate Shrink Factor")

        wm = context.window_manager
        if wm.uv_zoom_helper_estimate_for == mesh.name:
            box = layout.box()
            box.label(text=f"UV span: {wm.uv_zoom_helper_estimate_span_x:.4g} x {wm.uv_zoom_helper_estimate_span_y:.4g}")
            box.label(text=f"Suggested factor: {wm.uv_zoom_helper_estimate_factor:.4g}x")

        layout.operator("uv.rescale_zoom_shrink", icon='SHADING_RENDERED', text="Shrink UVs (auto)")


classes = (
    UV_OT_RescaleZoomEstimate,
    UV_OT_RescaleZoomShrink,
    UV_OT_RescaleZoomRestore,
    UV_PT_RescaleZoomPanel,
)


def register():
    bpy.types.WindowManager.uv_zoom_helper_estimate_for = bpy.props.StringProperty(default="")
    bpy.types.WindowManager.uv_zoom_helper_estimate_span_x = bpy.props.FloatProperty(default=0.0)
    bpy.types.WindowManager.uv_zoom_helper_estimate_span_y = bpy.props.FloatProperty(default=0.0)
    bpy.types.WindowManager.uv_zoom_helper_estimate_factor = bpy.props.FloatProperty(default=1.0)
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.uv_zoom_helper_estimate_for
    del bpy.types.WindowManager.uv_zoom_helper_estimate_span_x
    del bpy.types.WindowManager.uv_zoom_helper_estimate_span_y
    del bpy.types.WindowManager.uv_zoom_helper_estimate_factor


if __name__ == "__main__":
    register()