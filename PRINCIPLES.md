# Pandect guiding principles

These principles guide every Pandect change, feature, interface, and design.
Apply them when planning, implementing, and reviewing work.

1. **Keep it simple.** Choose the simplest correct solution that meets the actual
   need. Favor clear workflows, readable code, and understandable behavior.

2. **Be user friendly.** Design around the investigator’s task and next useful
   action. Use plain language, thoughtful defaults, accessible controls, and
   helpful recovery when something goes wrong.

3. **Make interactions intuitive.** Make it clear where users are, what they are
   viewing, and what they can do next. Follow familiar conventions and simplify
   confusing interactions before adding instructions.

4. **Do not overengineer.** Solve demonstrated needs. Introduce abstractions,
   configuration, services, and infrastructure only when their benefits justify
   their complexity and maintenance cost.

5. **Do not add bloat.** Every feature, dependency, control, and piece of content
   should earn its place through clear value. Reuse established capabilities and
   avoid unnecessary duplication.

6. **Give important information priority.** Prominently display information that
   helps users understand the current task or make a decision. Retain supporting
   details through clearly labeled, accessible views or disclosures; do not
   discard information merely to simplify a screen. Keep decision-critical
   context visible.

7. **Make complex work feel simple.** Present analysis and processing through
   understandable actions, useful summaries, and clear progress. Users should not
   need to understand the implementation to complete routine tasks. Communicate
   uncertainty, limitations, failures, and recovery honestly.

8. **Explain complexity where it helps.** Use concise contextual help, accessible
   tooltips, and user-facing documentation when concepts need explanation. Keep
   essential instructions and consequential information visible beside the
   relevant action.

9. **Design one coherent product.** Use consistent terminology, layouts,
   components, visual styles, and interaction patterns. Pandect should feel as
   though one person thoughtfully designed and built it. Introduce differences
   only when the task warrants them.

Simplicity must preserve correctness, security, case isolation, evidence
integrity, accessibility, resource bounds, and recoverability.

## Changes require explicit consent

Agents must not edit, replace, rename, or delete this file, or weaken its consent
requirement, without the project owner’s direct, explicit consent to the specific
change. This includes editorial changes. General authorization to implement
features, refactor code, or maintain documentation is not consent to revise these
principles. Agents may propose revisions for approval without changing the file.
