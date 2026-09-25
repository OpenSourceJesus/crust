(* signed / and mod: truncating, and min_int / -1 wraps as in OCaml *)
let half x = x / 2
  [@@ensures result <= x || x < 0]
let safe_div a b = a / b
  [@@requires b <> 0]
let safer_div a b = a / b
  [@@requires b > 0]
let rem a b = a mod b
  [@@requires b <> 0]
let () = print_int (half 9); print_int (safe_div (-7) 2); print_int (rem (-7) 2);
  print_int (safe_div min_int (-1)); print_newline ()
