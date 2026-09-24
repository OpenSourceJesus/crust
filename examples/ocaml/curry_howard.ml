let theorem1 (x : 'a) : 'a = x

let theorem2 (pair : 'a * 'b) : 'b * 'a =
  let (a, b) = pair in (b, a)

type ('a, 'b) or_type = 
  | Left of 'a
  | Right of 'b

let modus_ponens_or (disj : ('a, 'b) or_type) (f : 'a -> 'c) (g : 'b -> 'c) : 'c =
  match disj with
  | Left a  -> f a
  | Right b -> g b

type void = | (* Empty variant representing False/Absurdity *)

let explode (contradiction : void) : 'a = 
  match contradiction with _ -> .

let non_contradiction (proof : 'a * ('a -> void)) : void =
  let (a, not_a) = proof in
  not_a a
