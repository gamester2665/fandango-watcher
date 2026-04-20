// Review / checkout page snapshot for purchaser invariant checks.
// Evaluated in-page; must return a plain object (same contract as the Python fallback).
() => {
  const bodyText = (document.body && document.body.innerText) || "";
  const title = document.title || "";

  const lines = bodyText.split(/\r?\n/).map((ln) => ln.trim()).filter(Boolean);
  const orderTotalLines = [];
  for (const ln of lines) {
    if (/\$\s*\d+[.,]\d{2}/.test(ln) && /total|order|due|amount|today/i.test(ln)) {
      orderTotalLines.push(ln);
    }
  }

  const benefitHits = [];
  const benefitRe =
    /(AMC\s+Stubs\s+A-List|A-List\s+Benefit|Stubs\s+A-List)/gi;
  let m;
  while ((m = benefitRe.exec(bodyText)) !== null) {
    benefitHits.push(m[0]);
  }

  return {
    bodyText,
    title,
    review_hints: {
      order_total_lines: orderTotalLines.slice(0, 20),
      benefit_phrase_hits: [...new Set(benefitHits)].slice(0, 10),
    },
  };
}
