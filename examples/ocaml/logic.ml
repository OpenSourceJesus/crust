type ('a, 'b) or_ = L of 'a | R of 'b
type void = |
type 'a not_ = 'a -> void

let curry (f : 'a * 'b -> 'c) (x : 'a) (y : 'b) : 'c = f (x, y)
let uncurry (f : 'a -> 'b -> 'c) (p : 'a * 'b) : 'c = let (x, y) = p in f x y
let contrapositive (f : 'a -> 'b) (nb : 'b not_) : 'a not_ = fun a -> nb (f a)
let dni (a : 'a) : 'a not_ not_ = fun na -> na a
let de_morgan (n : ('a, 'b) or_ not_) : 'a not_ * 'b not_ =
  ((fun a -> n (L a)), (fun b -> n (R b)))
let distrib (p : 'a * ('b, 'c) or_) : ('a * 'b, 'a * 'c) or_ =
  let (a, bc) = p in
  match bc with
  | L b -> L (a, b)
  | R c -> R (a, c)
let or_comm (d : ('a, 'b) or_) : ('b, 'a) or_ =
  match d with L a -> R a | R b -> L b
let or_idem (d : ('a, 'a) or_) : 'a = match d with L a -> a | x -> (match x with R a -> a | L a -> a)
let triple (p : 'a * 'b * 'c) : 'c * 'a = let (a, _, c) = p in (c, a)
let absurd_left (d : (void, 'a) or_) : 'a = match d with L v -> (match v with _ -> .) | R a -> a

type 'a stream_ish = Stop | More of 'a * 'a stream_ish
let head_or (d : 'a) (s : 'a stream_ish) : 'a = match s with Stop -> d | More (x, _) -> x
