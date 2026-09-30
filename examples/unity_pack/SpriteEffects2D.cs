// SpriteEffects2D -- the 2D GPU path's effect byte, from a script.
//
// Put this file in the Unity project so scripts that call it compile in the
// Editor (where it does nothing). tools/unity_pack.py rewrites the calls: in
// a pack made with --gpu-batch, gles3_batch.h's shader draws the effect on
// every sprite of the GameObject:
//
//     SpriteEffects2D.Set(gameObject, SpriteEffect2D.Flash, 0.8f);
//     SpriteEffects2D.Clear(gameObject);
//
// amount: 0..1 for Flash (toward white), Grayscale, HueShift (a turn) and
// Dissolve (the share of texels gone); Outline's is its width in texels
// (1..8), drawn in the sprite's tint inside its bounds.
using UnityEngine;

public enum SpriteEffect2D { None, Flash, Grayscale, HueShift, Dissolve, Outline }

public static class SpriteEffects2D {
    public static void Set(GameObject go, SpriteEffect2D effect, float amount) { }
    public static void Clear(GameObject go) { }
}
