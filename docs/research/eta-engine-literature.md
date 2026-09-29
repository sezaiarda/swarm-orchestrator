# ETA engine: literature and recommended method

Research note, 2026-09-29. Scope: how to forecast when each campaign ("phase book") and the whole
ledger will finish, given a DAG of phases with heavy-tailed durations, `max_workers` slots, per-repo
serialization, a build semaphore, time gates, pauses, usage caps, owner-blocked rows and follow-ups
that appear mid-run.

URL check: every URL below was fetched when this list was compiled. Most returned 200; the ones marked
`(403)` are publisher pages that block scripted fetches (ScienceDirect, INFORMS, Wiley, Medium, PMI,
SAGE, T&F). Those were confirmed to exist through the search index, and a DOI is given where one
exists.

---

## 1. Makespan of a stochastic DAG under limited resources

| Source | Takeaway for us |
|---|---|
| Van Slyke, "Monte Carlo Methods and the PERT Problem", *Operations Research* 11(5):839-860, 1963. https://pubsonline.informs.org/doi/abs/10.1287/opre.11.5.839 (403). Report PDF: https://apps.dtic.mil/sti/tr/pdf/AD0412731.pdf (403) | This is the original argument for simulating the network instead of using PERT's closed form. Sampling every activity and recomputing the schedule gives the completion-time distribution, plus quantities PERT cannot produce: the probability each activity is critical, and the chance of finishing by a date. That is exactly the "what blocks what" view a swarm needs. |
| MacCrimmon & Ryavec, "An Analytical Study of the PERT Assumptions", *Operations Research* 12(1):16-37, 1964. https://pubsonline.informs.org/doi/10.1287/opre.12.1.16 (403). RAND version: https://www.rand.org/pubs/research_memoranda/RM3408.html (403) | This paper quantifies PERT's errors. The largest one is at the network level: PERT only follows the single longest path, so it is always optimistic when paths run in parallel and merge. A campaign ends when its **last** row finishes. That is a max over parallel chains, so any "sum of means along the critical path" ETA will come out early. |
| Hulett, "Project schedule risk analysis: Monte Carlo simulation or PERT?", *PM Network* 14(2), 2000. https://www.pmi.org/learning/library/project-schedule-risk-analysis-simulation-4620 (403). Also "Schedule risk analysis simplified", *PM Network* 10(7), 1996. https://www.pmi.org/learning/library/schedule-risk-analysis-simplified-10573 | This is the practitioner statement of **merge bias**: where paths converge, the project carries more risk than even its riskiest incoming path, and PERT understates it. His rule of thumb is a few thousand iterations for working estimates and about 10k for final reports. We only need a few percentiles, so 500 to 1000 runs is enough (see §6.4). |
| Graham, "Bounds on Multiprocessing Timing Anomalies", *SIAM J. Appl. Math.* 17(2):416-429, 1969. https://people.irisa.fr/Sophie.Pinchinat/AA/Graham1969SIAM.pdf | For list scheduling on m identical workers, makespan is at most (2 - 1/m) times optimal. The paper also proves **anomalies**: more workers, shorter tasks or fewer precedence constraints can each make the makespan *longer*. Two consequences for us. (a) The ETA has to come from simulating the swarm's **actual** pick rule, not from a formula. (b) `max(critical path, total work / m)` is a cheap lower bound that can serve as a sanity check or an instant placeholder. |
| Möhring, Radermacher & Weiss, "Stochastic scheduling problems I: General strategies", *Z. Oper. Res.* 28:193-260, 1984. https://link.springer.com/article/10.1007/BF01919323 | This is the formal basis for stochastic RCPSP. A schedule under random durations is a **policy**: at each finish event, decide what to start using only what has already happened. Our supervisor is exactly such a non-anticipative policy, and an event-driven simulation that calls the same decision rule at each simulated finish is the correct model of it. |
| Ballestín & Leus, "Resource-Constrained Project Scheduling for Timely Project Completion with Stochastic Activity Durations", *Production and Operations Management* 18(4), 2009. https://journals.sagepub.com/doi/10.1111/j.1937-5956.2009.01023.x (403). Kolisch & Hartmann, "Experimental investigation of heuristics for RCPSP: an update", *EJOR* 174(1):23-37, 2006. https://www.hsba.de/fileadmin/user_upload/bereiche/_dokumente/6-forschung/profs-publikationen/Hartmann_2006_Experimental_investigation_of_Heuristics.pdf | This is the SRCPSP and RCPSP heuristics literature. Its point is *optimizing* the policy, which we do not need. What we can use: priority-rule list scheduling (a serial or parallel "schedule generation scheme") is the standard and fast way to evaluate one sampled scenario, and heuristics are compared on sampled scenarios, which is the same Monte Carlo loop we would run. |

## 2. Per-task durations from heterogeneous history

| Source | Takeaway for us |
|---|---|
| Trietsch, Mazmanyan, Gevorgyan & Baker, "Modeling activity times by the Parkinson distribution with a lognormal core: Theory and validation", *EJOR* 216(2):386-396, 2012. https://www.sciencedirect.com/science/article/abs/pii/S0377221711007107 (403). Author PDF: http://mba.tuck.dartmouth.edu/pss/notes/lognormalparkinson.pdf | This is the empirical case, validated on field data, that project activity times are **lognormal**, with two corrections. First, recorded times are distorted by rounding and by the Parkinson effect (early finishes get hidden). Our workers finish as soon as they are done, so that effect should be small. Second, and more important, **durations are positively correlated (linear association)**: a common factor makes many activities slow together. Monte Carlo with independent draws therefore gives intervals that are too narrow. The fix is a per-run shared multiplier (§6.1). |
| Bernhardsson, "Why software projects take longer than you think: a statistical model", blog, 2019. https://erikbern.com/2019/04/15/why-software-projects-take-longer-than-you-think-a-statistical-model.html | This is the practitioner version. Estimates track the **median** well, but a lognormal blow-up factor makes the **mean**, and above all the sum of many tasks, dominated by the tail. For a sum of heavy-tailed durations, P85 can be several times P50 in the worst-behaved group. Report quantiles, not means, and fit on log(duration). |
| Harchol-Balter & Downey, "Exploiting process lifetime distributions for dynamic load balancing", *ACM TOCS* 15(3):253-285, 1997. https://users.soe.ucsc.edu/~scott/courses/Fall11/221/Papers/Sync/harcholbalter-tocs97.pdf | Under heavy tails, **a job that has already run a long time is expected to run longer still**, because mean residual life grows with age. For a phase that is running right now, the remaining time has to be sampled from the duration distribution *conditioned on elapsed > age*. Drawing a fresh duration, or taking "median minus elapsed", systematically underestimates the stragglers, and the stragglers are what set the campaign ETA. |
| Gelman, "Multilevel (Hierarchical) Modeling: What It Can and Cannot Do", *Technometrics* 48(3), 2006. https://sites.stat.columbia.edu/gelman/research/published/multi2.pdf. Efron & Morris, "Stein's Paradox in Statistics", *Scientific American* 236:119-127, 1977. https://www.scientificamerican.com/article/steins-paradox-in-statistics/ | This is **partial pooling**. A group's estimate is a precision-weighted blend of its own mean and its parent's mean, so a campaign with 3 samples mostly borrows from its row-type and global means, while one with 60 samples mostly speaks for itself. Gelman shows multilevel models predict well at both levels, even for groups with little data, which is our situation: a modest number of samples spread over many campaigns. |
| Smith, Foster & Taylor, "Predicting Application Run Times Using Historical Information", JSSPP 1998, LNCS 1459. https://www.globus.org/sites/default/files/runtime.pdf. Tsafrir, Etsion & Feitelson, "Backfilling Using System-Generated Predictions Rather than User Runtime Estimates", *IEEE TPDS* 18(6):789-803, 2007 (DOI 10.1109/TPDS.2007.70606) | This is the HPC evidence that **history-based prediction by "templates"** works: group past jobs by attribute combinations such as user, queue and executable, and predict from the most specific template that has enough data. Smith et al. choose which attributes define "similar" by searching for the combination that predicts best on held-out data. Tsafrir et al. show that even a trivial predictor (the mean of a user's last two jobs) beats user-supplied estimates. For us, cross-validation decides the features, such as description length and number of repos in `dir`, and nothing is hand-picked. |
| Flyvbjerg, "Curbing Optimism Bias and Strategic Misrepresentation in Planning: Reference Class Forecasting in Practice", *European Planning Studies* 16(1), 2008. https://www.researchgate.net/publication/233258056_Curbing_Optimism_Bias_and_Strategic_Misrepresentation_in_Planning_Reference_Class_Forecasting_in_Practice (403). Applied: Flyvbjerg, Hon & Fok, "Reference class forecasting for Hong Kong's major roadworks projects", 2016. https://arxiv.org/pdf/1710.09419. Critique and agenda: "Reference class forecasting: promises, problems, and a research agenda", *Production Planning & Control*, 2025. https://www.tandfonline.com/doi/full/10.1080/09537287.2025.2578708 (403) | This is the **outside view**: forecast a new item from the empirical distribution of comparable past items, not from its own story. Our pooled groups are the reference classes. The 2025 review names the practical failure: picking the reference class, which is too broad and loses signal, or too narrow and has too few samples. Partial pooling is the principled answer to that trade-off. |
| Jørgensen, Teigen & Moløkken, "Better sure than safe? Over-confidence in judgement based software development effort prediction intervals", *J. Systems and Software* 70(1-2):79-93, 2004. https://www.sciencedirect.com/science/article/abs/pii/S0164121202001607 (403) | Stated prediction intervals in software are routinely far **too narrow**. Our intervals come from data, not judgement, but the same failure appears if the model leaves out correlation or scope growth. Calibrate the intervals against a backtest (§7), not by eye. |

## 3. Forecasting remaining work: throughput Monte Carlo vs per-task simulation

| Source | Takeaway for us |
|---|---|
| Magennis, *Forecasting and Simulating Software Development Projects: Effective Modeling of Kanban & Scrum Projects using Monte-carlo Simulation*, 2011 (ISBN 9781466454835). Tools: https://focusedobjective.com/. Notebook: https://observablehq.com/@troymagennis/introduction-to-monte-carlo-forecasting | This is **throughput Monte Carlo**: repeatedly sample historical per-day (or per-week) completion counts until the remaining backlog is consumed, then report the percentiles of the finish date. His spreadsheet runs 500 trials. It needs no durations and no DAG, and it is robust. Its blind spot is that it assumes future work can proceed at the historical *aggregate* rate. That fails when the remaining work is one long serialized chain in one repo, which is common for us at the tail of a campaign. |
| Vacanti, *When Will It Be Done? Lean-Agile Forecasting to Answer Your Customers' Most Important Question*, Leanpub, 2019. https://leanpub.com/whenwillitbedone | Report forecasts as **percentiles, P50/P85/P95**, and track **item age** of work in progress against historical cycle-time percentiles. An in-flight item already older than P85 is the early warning. It also treats single-item forecasts (from cycle time) and multi-item forecasts (from throughput Monte Carlo) as different questions. |
| Singh, "All Models are Wrong, but Some are Random…", Towards Data Science, 2021. https://medium.com/data-science/all-models-are-wrong-but-some-are-random-25ff1491406f (403) | This is a **backtest** of five throughput-sampling variants: plain random sampling, Markov-chain, weekday-matched and others. Plain random sampling of history won. Lesson: sophistication in the sampler does not pay, and the backtest decides. |
| Expedia Group Technology, "Monte Carlo Forecasting in Software Delivery", 2020s. https://medium.com/expedia-group-tech/monte-carlo-forecasting-in-software-delivery-474bb49cb3f9 (403) | This is a practitioner write-up of running throughput Monte Carlo at team scale and communicating results as "N% likely by date D". |

**How throughput MC compares with per-task simulation for us:** throughput MC is what the dashboard
approximates today (one rate from the last 20 finishes). It will stay accurate while many rows are
ready in parallel across many repos. It goes wrong in exactly the situations that matter here:
a campaign whose remaining rows are serialized in one repo, rows waiting on `after:` gates, rows
behind the owner, and per-campaign ETAs when campaigns share slots. That is the argument for
per-task DAG simulation (§6). Throughput MC is worth keeping as a fallback and as a baseline the new
engine must beat in the backtest.

## 4. Groups (campaigns) in a shared-resource DAG

| Source | Takeaway for us |
|---|---|
| Browning & Yassine, "Resource-constrained multi-project scheduling: Priority rule performance revisited", *Int. J. Production Economics* 126(2):212-228, 2010. https://www.sciencedirect.com/science/article/abs/pii/S0925527310000915 (403) | This is the multi-project version, where several projects share resources and the objective is **per-project lateness** as well as portfolio lateness. Across 12,320 instances, the priority rule decides which project wins. So a campaign's ETA depends on the tie-break rule as much as on its own work, and it is only meaningful if the simulation uses the swarm's real pick order. |
| "Resource-constrained multi-project scheduling problem: A survey", *EJOR* 309(3):958-976, 2023. https://ideas.repec.org/a/eee/ejores/v309y2023i3p958-976.html | This is a current RCMPSP survey. It confirms the standard framing: projects are subsets of one combined activity network with shared resources, and each project's completion is the finish of its last activity. That is our definition: **campaign ETA = the time its last row finishes in the simulated shared schedule**. |
| Khoshkonesh et al., "Bayesian-Monte Carlo Schedule Updating for Construction Digital Twins", arXiv:2605.17608, 2026. https://arxiv.org/abs/2605.17608 | This is the closest recent end-to-end match: lognormal activity durations, updated by Bayes as activities complete, then propagated through the network by Monte Carlo into completion-time percentiles and criticality. It beats deterministic CPM and static probabilistic schedules on PSPLIB networks. It is structurally the same as our recommendation: refit on every finish, re-simulate. |

## 5. Calendars, blocked tasks, scope growth, censoring

| Source | Takeaway for us |
|---|---|
| Kreter, Rieck & Zimmermann, "Models and solution procedures for the RCPSP with general temporal constraints and calendars", *EJOR* 251(2):387-403, 2016 (DOI 10.1016/j.ejor.2015.11.021). https://www.sciencedirect.com/science/article/abs/pii/S037722171501070X (403) | This is **break calendars** in scheduling. A resource is unavailable in given windows, and activities are either interruptible or must not straddle a break. For us: the overnight stop and cap pauses are calendar breaks on *launching*. Whether a running worker is interrupted depends on what the supervisor actually does (it stops launching, and running phases finish), and the simulator has to encode the same rule. Minimum and maximum time lags are the formal form of `after:` / not-before gates. |
| Magennis, "Chapter 8: Estimating 'What Else' (scope growth)", *Forecasting using data*, Medium. https://medium.com/forecasting-using-data/chapter-8-estimating-what-else-scope-growth-dec308d7d37f (403) | This models **scope growth and split rate** explicitly: the backlog grows as work is done, and ignoring that biases every forecast early. Treat growth as a sampled quantity measured from history, not a guess. Our follow-up rate per finished row is exactly this parameter. |
| Kaplan & Meier, "Nonparametric Estimation from Incomplete Observations", *JASA* 53(282):457-481, 1958. https://web.stanford.edu/~lutian/coursepdf/KMpaper.pdf | This is **censoring**. Phases still running, or abandoned, at training time are lower bounds on duration, not missing data. Dropping them biases the fit toward short durations, and the bias is worst for the long tail we most need. At minimum, keep them as censored observations. For a lognormal, a censored maximum-likelihood fit (a Tobit-style likelihood on log time) is straightforward. |
| Nissimov & Feitelson, "Probabilistic Backfilling", JSSPP 2007, LNCS 4942:102-115. https://link.springer.com/chapter/10.1007/978-3-540-78699-3_6 | A scheduler can plan with the **whole historical runtime distribution** instead of a point estimate, and reason about the probability that a job ends before a reservation. This supports keeping empirical residual distributions rather than point predictions. |
| Gneiting, Balabdaoui & Raftery, "Probabilistic forecasts, calibration and sharpness", *JRSS-B* 69(2):243-268, 2007. https://rss.onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-9868.2007.00587.x (403). Gneiting & Raftery, "Strictly Proper Scoring Rules, Prediction, and Estimation", *JASA* 102:359-378, 2007. https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf | This is how to **validate** a distribution forecast: maximize sharpness (narrow intervals) *subject to* calibration. Check calibration with a PIT histogram or empirical coverage (does the truth fall below P85 about 85% of the time?). Score with proper scoring rules: CRPS, or pinball loss at the reported quantiles. This is the acceptance test for §7. |

---

## 6. Recommended method

This is a **per-phase stochastic duration model plus an event-driven Monte Carlo replay of the real
scheduler policy**, refit on every finish and validated by a ledger backtest. The throughput-MC
estimate stays as a fallback and as the baseline to beat.

### 6.1 Duration model

**What to measure.** Each phase's *service time*: wall-clock from first launch to `done`, **minus
time spent in swarm-wide pauses** (overnight stop, cap pause). The ledger's git tick time (what
`pace.py` reads) gives only the end. The start has to come from the supervisor records (launch
events), which only cover what ran on this machine. Where only ticks exist, the sample can serve
throughput MC but not the duration fit. Include retries in the service time: a failed attempt
plus its relaunch is what the ETA actually experiences. Keep the attempt count too, for later.

**Regime.** Durations measured while several workers contend on the build semaphore already include
queueing for builds. Tag each sample with the `max_workers` in force (pace.py already reconstructs
this from `.swarm.toml` history). Do **not** simulate the build semaphore explicitly in v1, unless
the logs separate "waiting for build lock" from "working". Otherwise contention is counted twice.
If the logs do separate them, model build time as a separate lognormal segment that queues on a
1-slot resource. That is the only honest way to predict what happens when `max_workers` changes.

**Model: log-linear location, partial pooling, empirical residuals.**

```
y = log(service_seconds)
y = μ + a[campaign] + b[kind] + c[repo] + β·log1p(desc_chars) + β2·n_dirs + ε
kind ∈ {W wave, F fix, other}      a, b, c ~ N(0, τ²)  (ridge penalty ⇔ Gaussian prior ⇔ partial pooling)
```

* Fit it with a small ridge or mixed-effects regression. With a few hundred rows and a few dozen
  levels, it takes milliseconds (numpy least squares; no Stan needed). Choose the penalties, and
  whether a text feature earns its place, by **time-ordered cross-validation** (Smith et al.:
  let held-out error choose the similarity features). Expect `kind` and `campaign` to matter.
  Description length may add a little. Keep a feature only if it lowers held-out pinball loss.
* **Residuals:** do not assume ε is exactly normal. Resample from the empirical residuals of the
  fitted model, pooled globally or by `kind` if the spreads differ clearly. This keeps the real
  tail, including multi-hour outliers, without committing to a parametric shape. A lognormal is the
  parametric fallback (Trietsch et al.). Add a Pareto tail only if a QQ plot of the residuals shows
  the lognormal falling short in the top 5%.
* **Censoring:** phases still running, or killed, enter as right-censored observations (log time ≥
  elapsed). Easiest is a censored-normal maximum-likelihood fit of the location model, or at
  minimum, never drop long-running in-flight rows from the history.
* **Correlation:** draw one **per-run shared multiplier** `exp(N(0, σ_c²))` and apply it to every
  duration in that Monte Carlo run. It stands for a slow day, model degradation, or a bad dependency
  (the linear association in Trietsch et al.). Set σ_c from the backtest: it is the knob that fixes
  intervals that come out too narrow.
* **How many samples:** pooling makes this automatic, since a group's own mean gets weight
  n/(n + σ²/τ²). In practice a campaign's own effect becomes material at about 5 to 10 finished
  rows. Below that, the prediction is essentially its `kind` plus global mean. Fallback order:
  campaign+kind → kind → global; with fewer than about 30 total samples, use throughput MC only.
* **In-flight rows:** sample remaining time as `D | D > age`: draw from the model's distribution
  truncated at the current age, by rejection or inverse-CDF on the residual sample, and subtract
  the age. Heavy tails mean the old ones should get *longer* remaining times (Harchol-Balter &
  Downey).
* **Failures:** v1 bakes retries into service time. v2, only if the backtest shows the need: a
  pooled Beta-Binomial `p_fail` per kind, and on a sampled failure either a retry (another draw) or
  a terminal "blocked" that removes the row and its descendants from that run.

### 6.2 Simulation (one Monte Carlo run)

This is a discrete-event simulation with a heap of events: `finish(phase)`, `gate_open(phase)`
(`after:` dates, operator not-before), `calendar_edge` (pause start/end, overnight stop), and
`cap_reset`. At every event, call **the supervisor's own eligibility and pick function**, factored
out so the simulator imports it and does not reimplement it. Graham's anomalies and Browning &
Yassine both show that the pick order moves the answer.

Constraints honoured at launch:
1. `needs:` all done (including simulated follow-ups).
2. Free slot: running < `max_workers`.
3. Repo mutex: no other running row shares a repo in `dir`. This is the ledger rule, applied
   exactly as the scheduler applies it.
4. Time gates open.
5. Launch calendar open: not inside a scheduled pause. Running rows continue through a pause
   if that is what the supervisor does; their service time excludes the pause, so extend the
   finish by the overlap.
6. Usage cap. v1 is deterministic: if a cap pause is active now, launching resumes at the known
   reset time; otherwise assume no cap. v2: estimate cap consumption per worker-hour from the
   meters, then in each run stop launching when projected consumption reaches the cap, until the
   window resets. Add v2 only if the backtest shows cap pauses are a large error term.
7. Build semaphore: implicit (v1, see §6.1), or an explicit 1-slot resource for the build segment
   (v2).

Outputs per run: each phase's finish, each campaign's finish (the max over its rows, including
follow-ups spawned within it), the overall makespan, and which rows were on the path to each
campaign's last finish (for "what blocks this campaign").

**Owner-blocked rows.** An owner wait is unbounded and not a stochastic duration, so leave it out
of the ETA. Show a separate "waiting on you" line: that row, plus the rows and campaigns
transitively behind it. The campaign ETA then reads "ETA for the N rows not behind you: P50 …
P85 …", and the scheduler still runs the other rows. Optionally, also show a conditional ETA, "if
answered now": simulate with the owner row made ready at `now`. That tells the owner what their
answer is worth in hours.

**Scope growth.** After each simulated finish, spawn a follow-up with probability `p_follow`. Use a
Beta-pooled rate per campaign, fit from history (follow-ups are identifiable by their `follow-up`
provenance in the ledger). The follow-up lands in the same campaign and repo, `needs` its parent,
has kind F, and draws its duration from the F model. Cap the recursion depth (for example 3).
This is Magennis's split-rate idea and removes a systematic early bias. Show
the ETA with growth included. A tooltip can give the no-growth figure.

### 6.3 What to show

* Per campaign: **P50 and P85** finish as wall-clock times ("Thu 14:10, likely by Fri 09:30").
  P95 in the detail view. P50/P85 is the Kanban-forecasting convention (Vacanti, Magennis).
* Overall: the same, for the makespan of every row not behind the owner.
* A "waiting on you" line with the count of rows and campaigns held.
* Optional: criticality (the share of runs in which a row was on its campaign's final path), and
  the Vacanti age warning for in-flight rows older than their predicted P85.
* Campaign percentiles are marginal: campaign A's P85 and campaign B's P85 are not a joint
  statement. Do not add them up.

### 6.4 Compute budget

* **500 runs by default, 1000 for the detail view.** Quantile standard error ≈ √(p(1−p)/n):
  for P85 at n = 500, that is ±1.6 percentile points, far below the model error.
* Cost: roughly remaining rows × runs × (log heap + eligibility scan). For 200 remaining rows and
  500 runs, that is about 100k events. In pure Python with heapq and a precomputed
  repo→rows index, that is well under a second to a few seconds. Run it in a **background
  thread or process**, never on the 2-second render path.
* **Recompute only on change.** Key the cache on (ledger content hash, state hash of running rows,
  pause/cap state, model version). Refit the duration model only when a phase finishes.
  In-flight ages change continuously, so re-simulate on a slow timer (for example every 5 minutes)
  even without other changes.
* **Common random numbers:** seed run *i* with a fixed seed *i*, so that re-simulating after a
  small change does not make the displayed ETA jitter from Monte Carlo noise alone.
* While a recompute is pending, show the last result with its age. For a cold start, show the
  Graham lower bound `max(critical path of medians, remaining work / m)`, labelled as a floor.

## 7. Validation: backtest on our own ledger

1. **Replay points:** for a set of times *t* spread over history (for example every 6 h, or at
   every 10th finish), rebuild the ledger as of *t* from git (`git show <commit-at-t>:<ledger>`;
   pace.py already walks this history), the done set, and which rows were in flight with their ages.
2. **Train only on data before *t*** (duration model, `p_follow`, σ_c). No peeking.
3. **Predict** each open campaign's finish and the overall finish, with pauses and caps that were
   *known* at *t*. Replay actual pauses as a secondary diagnostic, to split model error from
   calendar error.
4. **Truth:** the actual time the campaign's last row (including follow-ups filed later) was
   ticked. Score two targets: (a) rows known at *t*, which isolates the duration and scheduling
   model; (b) the whole campaign, which tests scope growth. Leave out, or report separately,
   campaigns whose finish waited on the owner after *t*.
5. **Metrics:** empirical coverage (the fraction of truths below P50 and below P85, which should be
   about 0.50 and 0.85), a PIT histogram, and pinball loss at P50 and P85 or CRPS (Gneiting &
   Raftery). Compare against two baselines: today's rate-based ETA, and plain throughput MC. Ship
   only if the new engine beats both on pinball loss **and** is calibrated.
6. **Tune** σ_c (and, if needed, one global variance inflation) to bring coverage into line.
   Re-run the backtest when the scheduler rules change.

With a few hundred finished phases, this gives a few hundred scored forecasts, enough to see a
10-point coverage miss at P85.

## 8. Build order (smallest useful first)

1. Service-time extraction from the supervisor records, with pause exclusion and censoring. Reuse
   the pace.py cache pattern.
2. Duration model: ridge on log time, kind+campaign+repo, empirical residuals, conditional
   sampling for in-flight rows.
3. Event-driven simulator that calls the real eligibility and pick code: slots, repo mutex, gates,
   pause calendar, deterministic cap. Owner rows excluded.
4. Backtest harness plus the two baselines. Tune σ_c.
5. Follow-up spawning. Then v2 items (cap consumption model, explicit build semaphore, failure
   model), but only where the backtest shows them as the biggest remaining error.
