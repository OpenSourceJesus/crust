(* lists: recursion down a list, [@@variant l] its size *)
let rec length l = match l with [] -> 0 | _ :: t -> 1 + length t
  [@@ensures result >= 0] [@@variant l]
let rec sum l = match l with [] -> 0 | x :: t -> x + sum t
  [@@variant l]
let () = print_int (length [1; 2; 3]); print_int (sum [4; 5]); print_newline ()
