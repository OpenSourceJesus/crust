(* functions as values: named, closed lambdas, returned, stored *)
let rec fold f acc l = match l with [] -> acc | x :: xs -> fold f (f acc x) xs
let rec filter p l = match l with [] -> [] | x :: t -> if p x then x :: filter p t else filter p t
let compose f g x = f (g x)
let inc x = x + 1
let dbl x = x * 2
let pick b = if b then inc else dbl
let apply_pair p x = let (f, g) = p in f (g x)
let () =
  print_int (fold (fun a b -> a + b) 0 [1; 2; 3; 4]); print_newline ();
  print_int (fold (fun a b -> a * b) 1 [1; 2; 3; 4]); print_newline ();
  print_int (fold (fun a b -> a + b) 0 (filter (fun x -> x mod 2 = 0) [1; 2; 3; 4; 5; 6])); print_newline ();
  print_int (compose inc dbl 5); print_int (compose dbl inc 5); print_newline ();
  print_int ((pick true) 10); print_int ((pick false) 10); print_newline ();
  print_int (apply_pair (inc, dbl) 7); print_newline ()
