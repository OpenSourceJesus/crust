(* local functions: lifted, capturing, recursive, mutually recursive, nested *)
let sum_to n =
  let rec go i acc = if i > n then acc else go (i + 1) (acc + i) in
  go 1 0
let scaled k l =
  let rec walk l = match l with [] -> [] | x :: t -> (x * k) :: walk t in
  walk l
let parity n =
  let rec even k = if k = 0 then true else odd (k - 1)
  and odd k = if k = 0 then false else even (k - 1) in
  if even n then 0 else 1
let outer a =
  let add b = a + b in
  let twice b = add (add b) in
  twice 1
let rec sum l = match l with [] -> 0 | x :: t -> x + sum t
let () =
  print_int (sum_to 100); print_newline ();
  print_int (sum (scaled 3 [1; 2; 3])); print_newline ();
  print_int (parity 10); print_int (parity 7); print_newline ();
  print_int (outer 5); print_newline ()
