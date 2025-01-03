import string
import jax
import jax.numpy as jnp
from mbi import CliqueVector, Domain, Factor, junction_tree
from mbi.marginal_loss import clique_mapping
import functools
import collections

_EINSUM_LETTERS = list(string.ascii_lowercase) + list(string.ascii_uppercase)


def sum_product(factors: list[Factor], dom: Domain) -> Factor:
    """Compute the sum-product of a list of factors."""
    attrs = sorted(set.union(*[set(f.domain) for f in factors]).union(set(dom)))
    mapping = dict(zip(attrs, _EINSUM_LETTERS))
    convert = lambda d: "".join(mapping[a] for a in d.attributes)
    formula = ",".join(convert(f.domain) for f in factors) + "->" + convert(dom)
    #print(jnp.einsum_path(formula, *[f.values for f in factors]))
    values = jnp.einsum(
        formula,
        *[f.values for f in factors],
        optimize="dp",  # default setting broken in some cases
        precision=jax.lax.Precision.HIGHEST
    )
    return Factor(dom, values)


def logspace_sum_product(potentials: list[Factor], dom: Domain) -> Factor:
    maxes = [f.max(f.domain.marginalize(dom).attributes) for f in potentials]
    stable_potentials = [(f - m).exp() for f, m in zip(potentials, maxes)]
    return sum_product(stable_potentials, dom).log() + sum(maxes)


def brute_force_marginals(potentials: CliqueVector, total: float = 1) -> CliqueVector:
    P = sum(potentials.arrays.values()).normalize(total, log=True).exp()
    marginals = {cl: P.project(cl) for cl in potentials.cliques}
    return CliqueVector(potentials.domain, potentials.cliques, marginals)


def einsum_marginals(potentials: CliqueVector, total: float = 1) -> CliqueVector:
    inputs = list(potentials.arrays.values())
    return CliqueVector(
        potentials.domain,
        potentials.cliques,
        {
            cl: logspace_sum_product(inputs, potentials[cl].domain)
            .normalize(total, log=True)
            .exp()
            for cl in potentials.cliques
        },
    )


def message_passing(potentials: CliqueVector, total: float = 1) -> CliqueVector:
    """Message passing marginal inference."""
    domain, cliques = potentials.domain, potentials.cliques

    jtree = junction_tree.make_junction_tree(domain, cliques)[0]
    message_order = junction_tree.message_passing_order(jtree)
    maximal_cliques = junction_tree.maximal_cliques(jtree)

    mapping = clique_mapping(maximal_cliques, cliques)
    beliefs = potentials.expand(maximal_cliques)

    messages = {}
    for i, j in message_order:
        sep = beliefs[i].domain.invert(tuple(set(i) & set(j)))
        if (j, i) in messages:
            tau = beliefs[i] - messages[(j, i)]
        else:
            tau = beliefs[i]
        messages[(i, j)] = tau.logsumexp(sep)
        beliefs[j] = beliefs[j] + messages[(i, j)]

    return beliefs.normalize(total, log=True).exp().contract(cliques)


def message_passing_new(potentials: CliqueVector, total: float = 1) -> CliqueVector:
    """Message passing marginal inference."""
    domain, cliques = potentials.active_domain, potentials.cliques

    jtree = junction_tree.make_junction_tree(domain, cliques)[0]
    message_order = junction_tree.message_passing_order(jtree)
    # TODO: upstream this logic to message_passing_order function
    message_order = [(i, j) for i, j in message_order if len(set(i) & set(j)) > 0]
    maximal_cliques = junction_tree.maximal_cliques(jtree)

    mapping = clique_mapping(maximal_cliques, cliques)
    inverse_mapping = collections.defaultdict(list)
    incoming_messages = collections.defaultdict(list)
    potential_mapping = collections.defaultdict(list)

    for cl in cliques:
        potential_mapping[mapping[cl]].append(potentials[cl])
        inverse_mapping[mapping[cl]].append(cl)

    for i in range(len(message_order)):
        msg = message_order[i]
        for j in range(i):
            msg2 = message_order[j]
            if msg[0] == msg2[1] and msg[1] != msg2[0]:
                incoming_messages[msg].append(msg2)

    messages = {}
    for i, j in message_order:
        shared = domain.project(tuple(set(i) & set(j)))
        input_potentials = potential_mapping[i]
        input_messages = [messages[key] for key in incoming_messages[(i, j)]]
        inputs = input_potentials + input_messages
        messages[(i, j)] = logspace_sum_product(inputs, shared)

    beliefs = {}
    for cl in maximal_cliques:
        input_potentials = potential_mapping[cl]
        input_messages = [messages[key] for key in messages if key[1] == cl]
        inputs = input_potentials + input_messages
        for cl2 in inverse_mapping[cl]:
            beliefs[cl2] = (
                logspace_sum_product(inputs, domain.project(cl2))
                .normalize(total, log=True)
                .exp()
            )

    return CliqueVector(potentials.domain, cliques, beliefs)


def variable_elimination(
    potentials: CliqueVector, clique: tuple[str, ...], total: float = 1
) -> Factor:
    clique = tuple(clique)
    cliques = potentials.cliques + [clique]
    domain = potentials.active_domain
    elim = domain.invert(clique)
    elim_order, _ = junction_tree.greedy_order(domain, cliques, elim=elim)

    k = len(potentials.cliques)
    psi = dict(zip(range(k), potentials.arrays.values()))
    for z in elim_order:
        psi2 = [psi.pop(i) for i in list(psi.keys()) if z in psi[i].domain]
        psi[k] = sum(psi2).logsumexp([z])
        k += 1
    # this expand covers the case when clique is not in the active domain
    newdom = potentials.domain.project(clique)
    return (
        sum(psi.values())
        .expand(newdom)
        .normalize(total, log=True)
        .exp()
        .project(clique)
    )
