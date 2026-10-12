# Think Tank Task Brief

Candidate work for the think tank, as thought through by the operator.
The tank plans against the charter; this is the working catalog of task
areas, some already in `potential-projects.md`.

## Cross-cutting goals

- Build tools and skills the tank (or other agent teams) can reuse to
  complete tasks on the operator's behalf. Reusability is a first-class
  outcome, not a side effect.
- Where a task needs a model, the tank should know which HuggingFace
  model it needs, retrieve it through the HuggingFace API (it has access),
  and run it on a Google Colab instance.
- For training work: gather the data, then train on sharded Google Colab
  runtimes using transfer learning / fine-tuning on a HuggingFace model.

## Vision and measurement from photographs

ShaneFanX on Instagram uses mathematics to analyze photographs and
determine the heights of people. Push this beyond that use case:

- Determine radiuses, diameters, and other dimensions from photos.
- Analyze a piece of clothing to determine how to recreate it, then draw
  lines on a blank canvas to reproduce everything needed to make the item.
- Take a 3D object and determine how to recreate the folds used to build it.
- Origami as an application of the same spatial-reasoning skill.
- Stick figures demonstrating exercises, from which it produces a daily
  exercise routine matched to the operator's goals.

The operator notes LLMs struggle at this today.

## Hard problems the tank should also attack

Consider other tasks LLMs still struggle with in October 2026 and pick
targets where a reusable skill or tool pays off. Areas already flagged:
spatial reasoning from images (above), rhythm/syllable-structure work
(music section), and producing consistent daily-personalized output.

## Trading and finance

- Backtest algorithmic trading strategies.
- Invent strategies of its own.
- Find a source of live tick data.
- Backtest cryptocurrency strategies; possibly ForEx markets (scope open).
- Review Polymarket accounts to find top performers.
- Research what has been done with JEV and algorithmic trading; produce
  research or a build for the operator to review.

## Research and reverse-engineering

- Research on Google Patents and similar sources.
- Research the Fruit Fly brain connectome and use it to build something.
- Review the architecture of sites like https://www.skillmelo.ai,
  https://www.nerim.ai, and https://www.dreyx.com and attempt rebuilds.
- Review NahamSec's AI course (https://app.hackinghub.io/course/hacking-ai/purchase)
  and attempt to recreate the course through research of each lesson and
  content extracted from viewing the page.
- Extract course content and produce a lesson plan to learn the material
  without purchasing the course.

## Social platforms

- Search over posts on X (Twitter) and LinkedIn.
- Respond to relevant posts in an area of interest. Example: searching for
  AI security hiring, finding a hiring manager's job posting, reviewing the
  application, and replying to the post.
- Search for new job postings and apply on the operator's behalf using the
  resume and other provided information.

## Character and figure consolidation

- Consolidate as much information as possible on a book character (every
  writing the character appears in) or a historical figure (everything
  findable).
- Use that knowledge to inform a decision or review a piece of content.
- Motivation includes Steve Jobs's talk on communicating with a historical
  figure.

## Music

- Build a reusable tool that stems a piece of music across all detected
  instruments, then recreates each as a MIDI track and pieces them back
  together. The operator has a lot of music he wants in MIDI form.
- A finger drumming game, interactive, Web MIDI based. Users connect an
  MPC and play along with tracks like a Guitar Hero for 16-pad
  controllers. Upload a MIDI drum track and play along. Envision different
  sample packs.
- Generate lyrics on a specific topic using the syllabic structure (strong
  and weak syllables) of lyrics the operator provides.
- A MIDI drummer using a model: feed it MIDI tracks without drums and have
  it create drums in the style of the music. The operator can name the
  style, but the beat must fit the groove.
- Use the song title as rhythm: the syllabic structure of the title defines
  the guiding drum track.
- Extend KeyLattice (web app for custom hexagonal MIDI keyboard layouts,
  isomorphic layouts, alternate tunings, chord modes) with new features.

## Security and infrastructure

- Bug bounty hunting using /Users/poole86/Desktop/bug_bounty.
- Run weekly scans on a schedule for /Users/poole86/Desktop/ai_governance_corpus
  or bug_bounty.
- Connect to the operator's VPN in another state for bug bounty work and to
  access a Digital Ocean droplet.
- Assets available: /Users/poole86/Downloads/adversarial-ai-swarm-6.zip and
  /Users/poole86/Downloads/intel-forge-main.zip.
- Create new intel forges and update them regularly if necessary.

## Sports simulation

- The operator competed in AWS Agentic Football (testing at
  /Users/poole86/Downloads/afc-arena-run). Build a similar creation with
  basketball instead of soccer. Each of the five players' actions would be
  controlled by agent instructions; cohesion matters. Use AWS Nova models
  as in the competition.

## Daily media

- Generate a report of information around the world and relevant to the
  operator, including personal and family items.
- Create an audio clip with AI-generated vocals to assemble a morning show.
- That show should be what wakes the operator up every morning.

## Ciphers and puzzles

- Review historical secret writing and attempt to decode it, taking each
  character and trying multiple avenues.
- Solve ciphers from https://cryptiana.web.fc2.com/code/unsolved.htm.
- The operator is drawn to the Ludwig TV series and likes the idea of using
  the tank for puzzles.

## Personal health and lifestyle

- Diet, recipes, grocery lists, etc. to lower triglyceride levels, changing
  daily and weekly.
- Research restaurants in the operator's area or another area.
- Research travel destinations and create a per-day plan, identify areas to
  avoid, use Google Street View to view locations, attempt to live like a
  local for a day within Street View, and search Reddit/X about the
  location. Act as a personal travel guide.

## VPN exploration

- Use the VPN to connect in a different country, understand what is only
  accessible there, and determine whether anything of value can be gathered
  that is unreachable without it.

## Content and writing

- Review code.
- Research generative engine optimization (GEO) and apply it.
- Review the Malcolm Gladwell Masterclass PDF, research and understand the
  topics, then generate a piece of writing that fits one of the covered
  techniques.
- Mimic the newspaper advice-as-recipe column: respond to someone's
  situation with advice plus a recipe for a dish that relates to the issue.

## Data and model work

- Review all of the operator's emails and determine what is safe to delete
  based on criteria he provides.
- Take roughly 100 illustrations and fine-tune / transfer-learn a
  HuggingFace model to generate illustrations in that style.

## Family

- Help provide activities for the operator's three-year-old son Miles, and
  parenting tips on request.

## Build list

Websites and applications the operator wants the think tank to build:

- **Legwork**: drop a pin on a map and pay a nearby local to be your eyes
  on the ground: photographing something, checking an item at a shop,
  attending an event, or live video before you buy.
- **Trefoil**: a Rubik's Cube as a graph, visualizing sticker positions as
  intersections of concentric circles in one panel and as a 3D model in
  another.
- **Personal handwriting style deformation**: a neural network trained live
  in the browser on letters the user draws, learning a deformation field
  that predicts the personal style for the whole alphabet.
- **Smart contract data licensing**: a system where people contribute data
  for AI training and are compensated every time their dataset is licensed.
- **From Up North-style design blog**: a design inspiration blog in that
  style.
- **Animusic-style 3D music videos**: generative animated-music
  visualizations; the operator thinks this could draw interest today.
- **3D scenes to 3D video**: create 3D scenes, then generate 3D videos from
  a transcript with characters. A personal IT Crowd with AI characters,
  uploaded to YouTube, could prove profitable. The 3D scenes themselves
  would be fun to explore.