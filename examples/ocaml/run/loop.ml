(* tail recursion is iteration: a million steps in constant stack *)
let rec count i acc = if i = 0 then acc else count (i - 1) (acc + i mod 7)
let collatz_steps n =
  let rec go n k = if n = 1 then k else if n mod 2 = 0 then go (n / 2) (k + 1) else go (3 * n + 1) (k + 1) in
  go n 0
let () =
  print_int (count 1000000 0); print_newline ();
  print_int (collatz_steps 27); print_newline ()
