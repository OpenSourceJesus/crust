(* one polymorphic function and type, instantiated at several types *)
type 'a option_ = Nothing | Just of 'a
let rec map f l = match l with [] -> [] | x :: xs -> f x :: map f xs
let rec length l = match l with [] -> 0 | _ :: t -> 1 + length t
let get d o = match o with Nothing -> d | Just x -> x
let swap p = let (a, b) = p in (b, a)
let is_pos n = n > 0
let rec count_true l = match l with [] -> 0 | b :: t -> (if b then 1 else 0) + count_true t
let () =
  print_int (length [1; 2; 3]); print_int (length [true; false]);
  print_int (length [(1, true)]); print_newline ();
  print_int (get 0 (Just 5)); print_int (get 7 Nothing);
  print_int (if get false (Just true) then 1 else 0); print_newline ();
  print_int (count_true (map is_pos [3; -1; 4; 0])); print_newline ();
  let (b, a) = swap (1, 2) in print_int (a * 10 + b); print_newline ()
