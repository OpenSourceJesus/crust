(* tail recursion compiles to a loop, proved as the recursion it is *)
let count_up n =
  let rec go i acc = if i >= n then acc else go (i + 1) (acc + 2)
    [@@requires i >= 0 && i <= n && acc >= 0] [@@ensures result >= acc] [@@variant n - i] in
  go 0 0
  [@@requires n >= 0]
let rec down i acc = if i <= 0 then acc else down (i - 1) (acc + 1)
  [@@requires i >= 0 && acc >= 0] [@@ensures result >= acc] [@@variant i]
let () = print_int (down 10 0); print_int (count_up 5); print_newline ()
