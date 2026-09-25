(* recursion, proved by induction on a [@@variant] *)
let rec sum_to n = if n <= 0 then 0 else n + sum_to (n - 1)
  [@@requires n >= 0] [@@ensures result >= n] [@@variant n]
let rec dist n = if n <= 0 then 0 else 1 + dist (n - 1)
  [@@requires n >= 0] [@@ensures result <= n && result >= 0] [@@variant n]
let () = print_int (sum_to 10); print_int (dist 7); print_newline ()
