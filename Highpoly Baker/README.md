# Highpoly baker - GRILLEN
This is a Blender add-on named “GRILLEN.” It helps bake texture maps from detailed high-poly meshes onto a low-poly mesh.

It can:
Bake normal, ambient-occlusion, diffuse/color, roughness, metallic, and alpha maps.
Use one or many high-poly source objects.
Create a shrink-wrapped “cage” mesh to control ray casting.
Auto-estimate ray distances, warn about UV overlaps, optionally apply scale, and save PNG outputs.
Build and assign a Blender material using the baked maps.
Process a queue of high/low-poly pairs.
Improve normal/alpha maps with 2× supersampling; normal vectors are correctly re-normalized after downsampling.



v2.0.0 — four new export controls in the Output sub-panel:

Format — PNG, TGA, or EXR dropdown. File extension changes automatically (.png, .tga, .exr).

Bit Depth — 8-bit, 16-bit, or 32-bit. Automatically clamped to what the format supports: TGA is always 8-bit (greyed out), PNG caps at 16-bit, EXR starts at 16-bit. Uses save_render() instead of save() so the scene's color depth setting is respected.

Normal Convention — OpenGL (Y+) or DirectX (Y-). When set to DirectX, the green channel is inverted after baking and before saving (1.0 - green per pixel). This happens transparently — you always bake in Blender's native OpenGL format, and the flip is applied as a post-process. Useful for Unreal Engine, 3ds Max, and any DirectX-convention pipeline.

Pack ORM — checkbox. After all passes complete, if any combination of AO, Roughness, or Metalness was baked, packs them into a single RGB image: R=AO, G=Roughness, B=Metallic. Saves as ObjectName_ORM.png/tga/exr alongside the individual maps. Missing channels default to white (1.0). The standard format for Unreal Engine and optimised PBR workflows that use a single ORM texture instead of three separate ones.

How it works:

ORM Image Texture — labeled "ORM (packed)" with a custom amber/brown color so it stands out from the individual map nodes above it
Separate Color — splits the packed texture into R, G, B channels
R (AO) — feeds into a Multiply node that's inserted between the existing Base Color source and the BSDF, same as the individual AO wiring
G (Roughness) — replaces any existing Roughness connection on the BSDF
B (Metallic) — replaces any existing Metallic connection on the BSDF

The individual AO/Roughness/Metalness texture nodes are still created above (for reference and for non-ORM workflows), but the ORM's Separate Color connections override them on the BSDF inputs. This way you can toggle ORM on/off between bakes and either wiring works.

v1.10.0 — new Bake Active Node feature. Here's how it works:

Workflow:

Select your mesh object
Open the Shader Editor and click the node whose output you want to flatten (e.g. an RGB Curves, Hue/Saturation, Color Ramp, Mix node — anything with a Color or Value output)
In the Grillen N-panel, click Bake Active Node in the new NODE BAKE section

v1.9.8 — the toggle is renamed to "Only Rebake Changed" with a clearer tooltip, and the underlying logic is fixed

v1.9.7 — reinstall to see the change. Here's what's different:

Each map's extra settings are now inside their own boxed sub-section with a header label and icon:

NORMAL MAP box (with a face-normal icon) appears under the toggles when Normal is enabled, containing just "Supersample (2×)"
ALPHA MAP box (with an alpha-image icon) appears when Alpha is enabled, containing the Alpha Source dropdown, "Supersample (2×)", and Edge Smoothing

v1.9.6 — Normal map now has its own Supersample (2×) toggle, shown when the Normal map is enabled (off by default, since normal maps are usually less prone to visible aliasing than alpha cutouts, and doubling bake time isn't always worth it).

v1.9.5 — the bug was remarkably subtle.

v1.9.4 — two new controls appear under the Alpha toggle when it's enabled:

Supersample (2×) — on by default. Bakes at twice the target resolution (so 1024×1024 for a 512px result), then box-filter downsamples back to target. Each output texel averages 4 bake rays instead of 1, giving true geometric anti-aliasing at wire and perforation edges. Doubles bake time for the alpha pass only.

Edge Smoothing — default 1.2. After baking (and downsampling if supersample is on), applies a separable Gaussian blur to soften any remaining stairstepping. Pure numpy, takes ~0.06 seconds at 512px. Setting to 0 disables it entirely. Values around 1.0–2.0 work well for most wire/net geometry.

v1.9.3 — here's the complete diagnosis and what changed:

The actual problem — far-wall hits on a closed high-poly

Rays from the +Y and +Z low-poly faces shoot inward. At the texel positions that correspond to holes in the perforated near face, the ray passes straight through to the far wall of the high-poly cube (0.55m away). Blender bakes that far-wall hit as solid white — which is wrong, it should be black (a hole). The ±X faces had their single center ray land directly on a hole, so they missed entirely.

The fix — auto-calculated max_ray_distance for geometry alpha

The addon now automatically limits how far alpha bake rays can travel. Using find_nearest it measures the actual gap between low-poly and high-poly surfaces, then sets:
  cage_extrusion = the measured gap (to push origins just outside the surface)
max_ray_distance = gap × 3 (enough to catch the near face from any position, but nowhere near the far wall)

v1.9.2:

use_backface_culling = False — makes both sides of the mesh visible, so you see the inner face of the cube through the holes rather than nothing
HASHED blend mode instead of CLIP — CLIP gives a hard binary cutoff which looks blocky on fine wires; HASHED uses stochastic dithering which handles thin geometry edges much more gracefully without needing alpha_threshold tuning
