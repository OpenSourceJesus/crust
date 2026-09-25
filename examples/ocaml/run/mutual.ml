(* mutual recursion: types that refer to each other, functions that call each other *)
type expr = Num of int | Add of expr * expr | Block of stmt * expr
and stmt = Skip | Seq of stmt * stmt | Eval of expr
let rec count_e e = match e with
  | Num _ -> 1
  | Add (a, b) -> count_e a + count_e b
  | Block (s, r) -> count_s s + count_e r
  [@@ensures result >= 1] [@@variant e]
and count_s s = match s with
  | Skip -> 0
  | Seq (a, b) -> count_s a + count_s b
  | Eval e -> count_e e
  [@@ensures result >= 0] [@@variant s]
let rec even n = if n = 0 then true else odd (n - 1)
  [@@requires n >= 0] [@@variant n]
and odd n = if n = 0 then false else even (n - 1)
  [@@requires n >= 0] [@@variant n]
let () =
  print_int (count_e (Block (Seq (Eval (Num 2), Skip), Add (Num 3, Num 4))));
  print_int (if even 10 then 1 else 0); print_newline ()
