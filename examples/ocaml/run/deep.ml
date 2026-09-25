(* deep recursion and a large structure, shared *)
let rec build n acc = if n = 0 then acc else build (n - 1) (n :: acc)
let rec sum l = match l with [] -> 0 | x :: t -> x + sum t
let rec rev_append a b = match a with [] -> b | x :: t -> rev_append t (x :: b)
let big = build 100000 []
let () =
  print_int (sum big); print_newline ();
  print_int (sum (rev_append big big)); print_newline ()
