/* Struct-heavy walk -- field loads through pointers, no SIMD contracts. */
struct Node {
    int key;
    int val;
    struct Node *next;
};

int walk(struct Node *n, int rounds) {
    int acc = 0;
    int r;
    for (r = 0; r < rounds; r++) {
        struct Node *p = n;
        while (p) {
            acc += p->key * 3 + p->val;
            p = p->next;
        }
    }
    return acc;
}

int main(void) {
    struct Node nodes[64];
    int i;
    for (i = 0; i < 64; i++) {
        nodes[i].key = i;
        nodes[i].val = i * i;
        nodes[i].next = (i + 1 < 64) ? &nodes[i + 1] : 0;
    }
    return walk(nodes, 80000) % 256;
}
