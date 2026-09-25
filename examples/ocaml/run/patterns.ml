(* nested patterns, guards, literals, tuples of variants *)
type shape = Circle of int | Rect of int * int | Empty
let area s = match s with
  | Circle r -> 3 * r * r
  | Rect (w, h) when w = h -> w * w
  | Rect (w, h) -> w * h
  | Empty -> 0
let rec pairs l = match l with
  | x :: y :: rest -> (x + y) :: pairs rest
  | [x] -> [x]
  | [] -> []
let classify p = match p with
  | (0, 0) -> 0
  | (0, _) -> 1
  | (_, 0) -> 2
  | (a, b) when a = b -> 3
  | _ -> 4
let both a b = match (a, b) with
  | (Circle _, Circle _) -> 1
  | (Rect _, _) -> 2
  | (_, Empty) -> 3
  | _ -> 4
let rec print_all l = match l with [] -> print_newline () | x :: t -> print_int x; print_int 9; print_all t
let () =
  print_int (area (Circle 2)); print_int (area (Rect (3, 3))); print_int (area (Rect (2, 5)));
  print_int (area Empty); print_newline ();
  print_all (pairs [1; 2; 3; 4; 5]);
  print_int (classify (0, 0)); print_int (classify (0, 5)); print_int (classify (5, 0));
  print_int (classify (4, 4)); print_int (classify (1, 2)); print_newline ();
  print_int (both (Circle 1) (Circle 2)); print_int (both (Rect (1, 1)) Empty);
  print_int (both Empty Empty); print_int (both Empty (Circle 1)); print_newline ()
