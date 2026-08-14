bl_info = {
    "name": "UV Rescale Zoom Helper",
    "author": "Mattias + Copilot",
    "version": (1, 0),
    "blender": (3, 0, 0),
    "location": "UV Editor > Sidebar > View Tab",
    "description": (
        "Blender's UV/Image editor has a hard zoom-out limit baked into its C code "
        "that can't be overridden from Python. This add-on works around it instead: "
        "it temporarily shrinks your UV coordinates around their center so a huge or "
        "sprawling layout fits inside the editor's normal zoom range, then restores "
        "them to their original scale afterward."
    ),
    "category": "UV",
}
 
import bpy
import bmesh
from mathutils import Vector
 
 
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
 
 
class UV_OT_RescaleZoomShrink(bpy.types.Operator):
    """Shrink the active UV layout around its center so it fits inside the editor's normal zoom range"""
    bl_idname = "uv.rescale_zoom_shrink"
    bl_label = "Shrink UVs"
    bl_options = {'REGISTER', 'UNDO'}
 
    factor: bpy.props.FloatProperty(
        name="Shrink Factor",
        description="Divide UV coordinates by this factor (e.g. 50 = shrink 50x smaller)",
        default=50.0,
        min=1.0001,
        soft_max=10000.0,
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
 
        for face in bm.faces:
            for loop in face.loops:
                uv = loop[uv_layer]
                uv.uv = pivot + (uv.uv - pivot) / self.factor
 
        bmesh.update_edit_mesh(mesh)
 
        # remember exactly how to undo this, even across a save/reload
        mesh["uv_zoom_helper_active"] = True
        mesh["uv_zoom_helper_factor"] = self.factor
        mesh["uv_zoom_helper_pivot"] = (pivot.x, pivot.y)
 
        self.report({'INFO'}, f"Shrunk UVs {self.factor}x around ({pivot.x:.3f}, {pivot.y:.3f})")
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
                uv.uv = pivot + (uv.uv - pivot) * factor
 
        bmesh.update_edit_mesh(mesh)
 
        del mesh["uv_zoom_helper_active"]
        del mesh["uv_zoom_helper_factor"]
        del mesh["uv_zoom_helper_pivot"]
 
        self.report({'INFO'}, f"Restored UVs (undid {factor}x shrink)")
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
            layout.label(text=f"Shrunk {mesh['uv_zoom_helper_factor']:.0f}x", icon='INFO')
            layout.operator("uv.rescale_zoom_restore", icon='LOOP_BACK')
        else:
            col = layout.column()
            col.operator("uv.rescale_zoom_shrink", icon='VIEWZOOM')
 
 
classes = (
    UV_OT_RescaleZoomShrink,
    UV_OT_RescaleZoomRestore,
    UV_PT_RescaleZoomPanel,
)
 
 
def register():
    for cls in classes:
        bpy.utils.register_class(cls)
 
 
def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
 
 
if __name__ == "__main__":
    register()