# Composer battery corpus

corpus_version: 0

Rules (spec §1): operator voice; task, never implementation; tight enough that one
shape is the reasonable reading; invented data; every prompt must NOT classify
EXPLICIT_MUTATION (tests/unit/evals/composer_battery/test_corpus.py enforces it).
The first unlabelled fenced block under each heading is sent byte-for-byte.

## canary

```
I've got a tiny list of three colours with a name and a hex code, sitting in a JSON file — just make it up.
I only want it read in and written straight back out to JSON, nothing else done to it.
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
Every application is first screened on its notes for passwords, secrets, or
anything confidential or internal; an application that fails that screening step is
an error, not one of the decisions below, and rather than being dropped it is set
aside in a quarantine file. The ones that pass get their columns renamed to
application_id, applicant_name and loan_amount, their notes shortened to forty
characters, and are then sorted out in this order: under 5000 is a micro loan; from
5000 up, a credit score of 700 or better and a mortgage is split by term, with 240
months or more long-term and anything shorter short-term, while a good score on any
other loan type is simply approved; below 700, an amount of 50000 or more is high
risk and everything else goes to manual review. Each outcome gets its own CSV file.
```

## error_routing

```
Make up a dozen sales deals — an id, the customer, the amount, a category and a
free-text notes field. Every deal is first screened on its notes for passwords,
secrets or confidential material; a deal that fails that screening step is an error
rather than a business outcome, and it must not be dropped — set it aside in a
quarantine file. The deals that pass carry on: their notes are shortened to fifty
characters, and then deals of ten thousand or more are split by category —
enterprise ones to their own file and the other large ones to a commercial file —
while everything under ten thousand goes to a standard file.
```

## schema_contracts_demo

```
I need a customer transaction feed handled with the column types stated up front:
customer_id is text, amount_usd is a number, transaction_date_time is text,
product_name is text, status_active_inactive is text and notes_comments is text.
Make the rows up. Every row travels through unchanged, and then anything with an
amount_usd of 500 or more goes to a high-value CSV file while the rest goes to the
ordinary output file.
```

## json_explode

```
Make up a few orders in JSON, each with an order id and a list of the items on it.
I want one row out per item rather than one per order, with the item kept under its
own field and its position in the list recorded next to it. Write the result out as
JSON.
```

## batch_aggregation

```
Make up twenty-odd expense records with an id, a date, a category, an amount and a
region. I don't want the individual rows out — take them five at a time and, for
each group of five, give me the count, the total and the average amount broken down
by category. Write those summaries out as CSV.
```

## statistical_batch_plugins

```
Make up a couple of dozen model prediction records — an id, the model name, the
label it predicted and the task family. Take them eight at a time and, for each
model in the batch, tell me its three most frequent predicted labels. Write those
out as JSON lines.
```

## deaggregation

```
Make up a dozen product lines with an id, a name, a category and a copies column
saying how many labels that product needs. Work through them three at a time and
give me one row per label — a product needing four labels appears four times — with
each copy numbered. Write the result out as CSV.
```

## report_assemble

```
I have a plain text file of log lines — make up something that looks like one, one
line per row and blank lines kept as they are. Collect the lines two at a time and
turn each pair into a small markdown report headed Run report, then write those
reports out as JSON.
```

## row_union_ab_experiment

```
Make up eight support tickets, each with an id, the ticket text and a baseline
quality score. I want every ticket tried both ways at once — a control arm that
keeps the baseline score and a treatment arm that scores twenty-five percent higher
— with each arm labelled, and then both labelled versions of a ticket brought back
into one stream together, two rows per ticket rather than one merged row. Once the
whole stream is through, compare the arms against the control and write the
comparison out as JSON lines.
```

## template_lookups

```
Make up a dozen support tickets — a ticket id, a category id, a priority id, a
subject and a body. Have a model classify each one against these categories:
billing (payments, refunds, subscriptions, invoices), technical (bugs, crashes,
performance) and general (feature requests, feedback, account questions), taking
the priority into account for the tone it recommends. Its answer goes on the row as
a classification field. Anything the model cannot classify goes to a quarantine
file instead of the results file; both come out as JSON.
```

## multi_query_assessment

```
Make up half a dozen clinical case studies, each with a user id and a background, a
symptoms and a history column. Have a model assess every case against five criteria
— diagnosis, treatment, prognosis, risk and follow-up — returning a score out of 100
and a short rationale for each criterion, all kept on the row. The assessed rows
come out as CSV; anything the model fails on goes to a quarantine file as JSON.
```

## openrouter_sentiment

```
Make up a dozen customer reviews, each with an id and the review text. I want the
sentiment of each one — positive, negative or neutral, with a confidence and a
one-line summary — worked out by a model and kept on the row. Reviews the model
fails on go to a quarantine file rather than being lost. Write the results out as
JSON lines.
```

## llm_source

```
I have no data for this one. The rows should come from a model instead: ask it for
five plausible support tickets, one per row, and keep whatever it returns as it
comes. Write them straight out as JSON lines.
```

## field_drop

```
I've got a tiny list of three colours — each one has a name and a hex code. Make the data up.
In the file that comes out I only want the colour names, not the hex codes. JSON both ends.
```
