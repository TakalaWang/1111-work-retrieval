import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const packageRoot = fileURLToPath(new URL('..', import.meta.url));
const temporaryDirectory = await mkdtemp(
  join(tmpdir(), 'work-retrieval-contract-')
);

try {
  const types = join(temporaryDirectory, 'types.d.ts');
  const result = spawnSync(
    'pnpm',
    ['exec', 'openapi-typescript', 'openapi.json', '-o', types],
    { cwd: packageRoot, stdio: 'inherit' }
  );
  if (result.status !== 0) process.exit(result.status ?? 1);

  const [committed, generated] = await Promise.all([
    readFile(join(packageRoot, 'types.d.ts')),
    readFile(types)
  ]);
  if (!committed.equals(generated)) {
    console.error(
      'types.d.ts is stale; run pnpm --filter @1111-work-retrieval/contract generate'
    );
    process.exitCode = 1;
  }
} finally {
  await rm(temporaryDirectory, { recursive: true, force: true });
}
