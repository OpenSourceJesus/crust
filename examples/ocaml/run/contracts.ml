(* contracts on compiled OCaml: `abs` wraps at min_int, and says so *)
let clamp lo hi x = if x < lo then lo else if x > hi then hi else x
  [@@requires lo <= hi] [@@ensures lo <= result && result <= hi]
let iabs x = if x < 0 then 0 - x else x
  [@@ensures result >= 0]
let iabs2 x = if x < 0 then 0 - x else x
  [@@requires x > min_int] [@@ensures result >= 0]
let () = print_int (clamp 0 10 42); print_int (iabs (-7)); print_int (iabs2 (-8)); print_newline ()
