export interface TaxonomyOption {
  code: string;
  path: readonly string[];
}

export interface TaxonomyRow {
  key: string;
  path: string[];
  option?: TaxonomyOption;
  hasChildren: boolean;
}

export function childRows(
  options: readonly TaxonomyOption[],
  currentPath: readonly string[]
): TaxonomyRow[] {
  const nodes = new Map<
    string,
    { path: string[]; options: TaxonomyOption[]; hasChildren: boolean }
  >();

  for (const option of options) {
    if (
      option.path.length <= currentPath.length ||
      !currentPath.every((part, index) => option.path[index] === part)
    )
      continue;

    const path = option.path.slice(0, currentPath.length + 1);
    const key = path.join('\u0000');
    const node = nodes.get(key) ?? { path, options: [], hasChildren: false };
    if (option.path.length === path.length) node.options.push(option);
    else node.hasChildren = true;
    nodes.set(key, node);
  }

  return [...nodes.entries()].flatMap(([pathKey, node]) =>
    node.options.length > 0
      ? node.options.map((option) => ({
          key: option.code,
          path: node.path,
          option,
          hasChildren: node.hasChildren
        }))
      : [
          {
            key: `path:${pathKey}`,
            path: node.path,
            hasChildren: node.hasChildren
          }
        ]
  );
}
