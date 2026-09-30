// SpriteEffects2D -- the 2D GPU path's effect byte, from a Godot C# script.
//
// Put this file in the Godot project so scripts that call it compile in the
// editor (where it does nothing). tools/godot_pack.py rewrites the calls: in
// a pack made with --gpu-batch, gles3_batch.h's shader draws the effect on
// every sprite of the node and of its children (as a node's modulate is its
// children's):
//
//     SpriteEffects2D.Set(this, SpriteEffect2D.Flash, 0.8f);
//     SpriteEffects2D.Set(GetNode<Enemy>("../Enemy"), SpriteEffect2D.Grayscale, 1f);
//     SpriteEffects2D.Clear(this);
//
// The node is `this` or a node reference (a GetNode<T> of a script's class
// or a Godot type, a field holding one). amount: 0..1 for Flash (toward
// white), Grayscale, HueShift (a turn) and Dissolve (the share of texels
// gone); Outline's is its width in texels (1..8), drawn in the sprite's tint
// inside its bounds. The Unity version is SpriteEffects2D.cs.
using Godot;

public enum SpriteEffect2D { None, Flash, Grayscale, HueShift, Dissolve, Outline }

public static class SpriteEffects2D
{
    public static void Set(Node node, SpriteEffect2D effect, float amount) { }
    public static void Clear(Node node) { }
}
