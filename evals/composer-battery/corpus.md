# Composer battery corpus

corpus_version: 0

Rules (spec §1): operator voice; task, never implementation; tight enough that one
shape is the reasonable reading; invented data; every prompt must NOT classify
EXPLICIT_MUTATION (tests/unit/evals/composer_battery/test_corpus.py enforces it).
The first unlabelled fenced block under each heading is sent byte-for-byte.

## canary

```
I've got a tiny list of three colours with a name and a hex code — just make it up.
I only want it read in and written back out as JSON, nothing else done to it.
```

## fork_coalesce

```
Make up three products, each with a sku, a name, a price and a long rambling description.
First trim every description down to forty characters. Then I want each product sent
down two parallel paths at the same time and the two copies brought back together
into one record per product, with each path's copy kept under its own key rather
than blended. A final yes/no check on the merged records that always passes should
sit before the output. Write the merged records out as JSON.
```

## transform_pipeline

```
Make up a handful of orders — an order id, a quantity and a unit price, all as
plain text the way a spreadsheet export would give them. First turn the quantity
and unit price into proper numbers, then work out a line total for each order,
and write the finished orders out as CSV.
```

## boolean_routing

```
Make up a short list of review comments — each with an id, the message text and a
plain true/false flag saying whether it was approved. The approved ones need to
end up in one CSV file and the rejected ones in another. Nothing else happens to
them on the way.
```

## explicit_routing

```
Invent a handful of customer transactions — an id, a customer name, an amount, a
currency and a region. The rows themselves stay exactly as they came in as they
pass through, and then anything worth five thousand or more should be written to a
high-value CSV file while everything else goes to a standard one.
```

## threshold_gate

```
Make up a dozen expense lines, each with an id, a name, an amount and a category.
Anything over 1000 goes to one CSV file and the rest goes to another. The amounts
are the only thing that decides it, and nothing about the rows changes.
```

## deep_routing

```
Make up a dozen loan applications with an id, the applicant's name, the amount, a
credit score, the loan type, the term in months, and a free-text notes field.
Notes that mention a password, a secret, or anything confidential or internal must
be pulled aside into a quarantine file. Everything else gets its columns renamed to
application_id, applicant_name and loan_amount, its notes shortened to forty
characters, and then sorted out in this order: under 5000 is a micro loan; from
5000 up, a credit score of 700 or better and a mortgage is split by term, with 240
months or more long-term and anything shorter short-term, while a good score on any
other loan type is simply approved; below 700, an amount of 50000 or more is high
risk and everything else goes to manual review. Each outcome gets its own CSV file.
```
