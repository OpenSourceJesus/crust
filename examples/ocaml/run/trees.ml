(* a binary tree: two recursive fields, and insert building new nodes *)
type tree = Leaf | Node of tree * int * tree
let rec size t = match t with Leaf -> 0 | Node (l, _, r) -> size l + 1 + size r
  [@@ensures result >= 0] [@@variant t]
let rec mirror t = match t with Leaf -> Leaf | Node (l, v, r) -> Node (mirror r, v, mirror l)
  [@@variant t]
let rec all_pos t = match t with Leaf -> true | Node (l, v, r) -> v > 0 && all_pos l && all_pos r
  [@@variant t]
let rec insert x t = match t with
  | Leaf -> Node (Leaf, x, Leaf)
  | Node (l, v, r) -> if x < v then Node (insert x l, v, r) else Node (l, v, insert x r)
  [@@ensures result <> Leaf] [@@variant t]
let t = insert 5 (insert 3 (insert 8 Leaf))
let () = print_int (size t); print_int (size (mirror t)); print_int (if all_pos t then 1 else 0); print_newline ()
