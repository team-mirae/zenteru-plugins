// Runs obfus-react's own engine from the command line (interop fixtures/tests).
//   node bridge.ts pack   IN OUT CHUNK_SIZE ECC(0|1)
//   node bridge.ts unpack IN OUT           -> prints {"container", "repaired", "damagedChunks"}
// run.sh copies obfus-react's src/engine next to this file first.
import { readFileSync, writeFileSync } from 'node:fs';
import { packSource, unpackSource, bytesSource, arraySink } from './engine/container.ts';
import { MemoryBudget } from './engine/budget.ts';

const [cmd, inp, out, chunk, ecc] = process.argv.slice(2);
const budget = new MemoryBudget(1 << 30);
const data = new Uint8Array(readFileSync(inp));
const sink = arraySink();
if (cmd === 'pack') {
  await packSource(bytesSource(data), { chunkSize: Number(chunk), ecc: ecc === '1', budget, sink });
  writeFileSync(out, Buffer.concat(sink.parts));
} else if (cmd === 'unpack') {
  const r = await unpackSource(bytesSource(data), { budget, sink });
  writeFileSync(out, Buffer.concat(sink.parts));
  console.log(JSON.stringify({ container: r.container, repaired: r.repaired, damagedChunks: r.damagedChunks }));
} else {
  console.error('usage: bridge.ts pack|unpack IN OUT [CHUNK ECC]');
  process.exit(2);
}
