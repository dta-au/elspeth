# Exact pushed checkpoint profile

Commit: ad773c656ff6009bf13e7916e1f249076e8da9b2

Status: qualified_complete_comparison; 3 pairs, 20 turns/run; controlled provider, SQLite.

Summed process CPU reduction: 22.6376%.

| Turn | Main CPU s | Feature CPU s | Main SQL | Feature SQL | Main RSS MiB | Feature RSS MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2.463 | 1.830 | 477 | 474 | 442.184 | 443.867 |
| 2 | 2.463 | 1.502 | 479 | 476 | 453.938 | 456.891 |
| 3 | 2.217 | 1.647 | 476 | 476 | 471.180 | 468.141 |
| 4 | 2.273 | 1.594 | 476 | 476 | 481.711 | 484.473 |
| 5 | 2.353 | 1.775 | 479 | 479 | 497.254 | 494.711 |
| 6 | 2.537 | 1.804 | 482 | 479 | 507.559 | 514.020 |
| 7 | 2.651 | 1.921 | 482 | 479 | 520.102 | 519.289 |
| 8 | 2.792 | 2.040 | 482 | 482 | 540.555 | 534.676 |
| 9 | 2.960 | 2.226 | 485 | 482 | 554.867 | 546.125 |
| 10 | 3.020 | 2.273 | 485 | 482 | 572.613 | 568.371 |
| 11 | 2.973 | 2.306 | 482 | 482 | 588.473 | 585.172 |
| 12 | 3.668 | 2.531 | 485 | 482 | 603.680 | 607.223 |
| 13 | 3.369 | 2.602 | 485 | 482 | 609.043 | 617.781 |
| 14 | 3.643 | 3.255 | 485 | 485 | 617.832 | 626.449 |
| 15 | 3.606 | 2.916 | 488 | 485 | 622.922 | 629.684 |
| 16 | 3.566 | 3.042 | 488 | 485 | 632.664 | 640.457 |
| 17 | 3.942 | 3.217 | 485 | 488 | 640.016 | 646.758 |
| 18 | 4.427 | 3.397 | 491 | 488 | 655.266 | 659.266 |
| 19 | 3.949 | 3.379 | 491 | 488 | 656.316 | 665.211 |
| 20 | 4.177 | 3.584 | 491 | 485 | 662.168 | 675.945 |

Medians across three repetitions. SQL counts are statements, not rows or wire bytes. RSS is process memory, not allocations or a leak diagnosis. No memory reduction established. Source, input, runtime and result parity qualified. No live-provider speedup claim.

Raw summary: work/.cache/common-main-pairs-final-2-summary.json. Both earlier and final-source runs remain preserved.
