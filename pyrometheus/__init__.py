__copyright__ = """
Copyright (C) 2020 Esteban Cisneros
Copyright (C) 2020 Andreas Kloeckner
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from numbers import Number
from functools import singledispatch

import pymbolic.primitives as p
from pymbolic.mapper.stringifier import StringifyMapper, PREC_NONE, PREC_CALL
import cantera as ct
import numpy as np  # noqa: F401

from mako.template import Template


# {{{ code generation helpers


class CodeGenerationMapper(StringifyMapper):
    def map_constant(self, expr, enclosing_prec):
        return repr(expr)

    def map_if(self, expr, enclosing_prec, *args, **kwargs):
        return "np.where(%s, %s, %s)" % (
            self.rec(expr.condition, PREC_NONE, *args, **kwargs),
            self.rec(expr.then, PREC_NONE, *args, **kwargs),
            self.rec(expr.else_, PREC_NONE, *args, **kwargs),
        )

    def map_call(self, expr, enclosing_prec, *args, **kwargs):
        return self.format(
            "np.%s(%s)",
            self.rec(expr.function, PREC_CALL, *args, **kwargs),
            self.join_rec(", ", expr.parameters, PREC_NONE, *args, **kwargs),
        )


def str_np_inner(ary):
    if isinstance(ary, Number):
        return repr(ary)
    elif ary.shape:
        return "[%s]" % (", ".join(str_np_inner(ary_i) for ary_i in ary))
    raise TypeError("invalid argument to str_np_inner")


def str_np(ary):
    return "np.array(%s)" % str_np_inner(ary)


# }}}


# {{{ polynomial processing


def nasa7_conditional(t, poly, part_gen):
    # FIXME: Should check minTemp, maxTemp
    return p.If(
        p.Comparison(t, ">", poly.coeffs[0]),
        part_gen(poly.coeffs[1:8], t),
        part_gen(poly.coeffs[8:15], t),
    )


@singledispatch
def poly_to_expr(poly):
    raise TypeError(f"unexpected argument type in poly_to_expr: {type(poly)}")


@poly_to_expr.register
def _(poly: ct.NasaPoly2, arg_name):
    def gen(c, t):
        assert len(c) == 7
        return c[0] + c[1] * t + c[2] * t ** 2 + c[3] * t ** 3 + c[4] * t ** 4

    return nasa7_conditional(p.Variable(arg_name), poly, gen)


@singledispatch
def poly_to_enthalpy_expr(poly, arg_name):
    raise TypeError("unexpected argument type in poly_to_enthalpy_expr: "
            f"{type(poly)}")


@poly_to_enthalpy_expr.register
def _(poly: ct.NasaPoly2, arg_name):
    def gen(c, t):
        assert len(c) == 7
        return (
            c[0]
            + c[1] / 2 * t
            + c[2] / 3 * t ** 2
            + c[3] / 4 * t ** 3
            + c[4] / 5 * t ** 4
            + c[5] / t
        )

    return nasa7_conditional(p.Variable(arg_name), poly, gen)


@singledispatch
def poly_to_entropy_expr(poly, arg_name):
    raise TypeError("unexpected argument type in poly_to_entropy_expr: "
            f"{type(poly)}")


@poly_to_entropy_expr.register
def _(poly: ct.NasaPoly2, arg_name):
    log = p.Variable("log")

    def gen(c, t):
        assert len(c) == 7
        return (
            c[0] * log(t)
            + c[1] * t
            + c[2] / 2 * t ** 2
            + c[3] / 3 * t ** 3
            + c[4] / 4 * t ** 4
            + c[6]
        )

    return nasa7_conditional(p.Variable(arg_name), poly, gen)


# }}}


# {{{ equilibrium constants


def equilibrium_constants_expr(sol: ct.Solution, react: ct.Reaction, gibbs_rt):
    indices_reac = [sol.species_index(sp) for sp in react.reactants]
    indices_prod = [sol.species_index(sp) for sp in react.products]

    # Stoichiometric coefficients
    nu_reac = [react.reactants[sp] for sp in react.reactants]
    nu_prod = [react.products[sp] for sp in react.products]

    sum_r = sum(nu_reac_i * gibbs_rt[indices_reac_i]
            for indices_reac_i, nu_reac_i in zip(indices_reac, nu_reac))
    sum_p = sum(nu_prod_i * gibbs_rt[indices_prod_i]
            for indices_prod_i, nu_prod_i in zip(indices_prod, nu_prod))

    # Check if reaction is termolecular
    sum_nu_net = sum(nu_prod) - sum(nu_reac)
    if sum_nu_net < 0:
        # Three species on reactants side
        return sum_p + p.Variable("C0") - sum_r
    elif sum_nu_net > 0:
        # Three species on products side
        return sum_p - (sum_r + p.Variable("C0"))
    else:
        return sum_p - sum_r

# }}}


# {{{ Rate coefficients

def arrhenius_expr(rate_coeff: ct.Arrhenius):
    raise NotImplementedError()

# }}}


# {{{ main code template

code_tpl = Template(
    """
import numpy as np

class Thermochemistry:
    model_name    = ${repr(sol.source)}
    num_elements  = ${sol.n_elements}
    num_species   = ${sol.n_species}
    num_reactions = ${sol.n_reactions}
    num_falloff   = ${
        sum(1 if isinstance(r, ct.FalloffReaction) else 0
            for r in sol.reactions())}

    one_atm = 1.01325e5
    gas_constant = 8314.4621
    big_number = 1.0e300

    species_names = ${sol.species_names}
    species_indices = ${
        dict([[sol.species_name(i), i]
            for i in range(sol.n_species)])}

    wts = ${str_np(sol.molecular_weights)}
    iwts = 1/wts

    def species_name(self, species_index):
        return self.species_name[species_index]

    def species_index(self, species_name):
        return self.species_indices[species_name]

    def get_specific_gas_constant(self, Y):
        return self.gas_constant * np.dot( self.iwts, Y )

    def get_density(self, p, T, Y):
        mmw = self.get_mix_molecular_weight( Y )
        RT  = self.gas_constant * T
        return p * mmw / RT

    def get_pressure(self, rho, T, Y):
        mmw = self.get_mix_molecular_weight( Y )
        RT  = self.gas_constant * T
        return rho * RT / mmw

    def get_mix_molecular_weight(self, Y):
        return 1/np.dot( self.iwts, Y )

    def get_concentrations(self, rho, Y):
        return self.iwts * rho * Y

    def get_mixture_specific_heat_cp_mass(self, T, Y):
        return self.gas_constant * np.sum(
            self.get_species_specific_heats_R(T)* Y * self.iwts)

    def get_mixture_specific_heat_cv_mass(self, T, Y):
        cp0_R = self.get_species_specific_heats_R( T ) - 1.0
        return self.gas_constant * np.sum(Y * cp0_R * self.iwts)

    def get_mixture_enthalpy_mass(self, T, Y):
        h0_RT = self.get_species_enthalpies_RT( T )
        return self.gas_constant * T * np.sum(Y * h0_RT * self.iwts)

    def get_mixture_internal_energy_mass(self, T, Y):
        e0_RT = self.get_species_enthalpies_RT( T ) - 1.0
        return self.gas_constant * T * np.sum(Y * e0_RT * self.iwts)

    def get_species_specific_heats_R(self, T):
        return np.array([
            % for sp in sol.species():
            ${cgm(poly_to_expr(sp.thermo, "T"))},
            % endfor
            ])

    def get_species_enthalpies_RT(self, T):
        return np.array([
            % for sp in sol.species():
            ${cgm(poly_to_enthalpy_expr(sp.thermo, "T"))},
            % endfor
            ])

    def get_species_entropies_R(self, T):
        return np.array([
            % for sp in sol.species():
                ${cgm(poly_to_entropy_expr(sp.thermo, "T"))},
            % endfor
            ])

    def get_species_gibbs_RT(self, T):
        h0_RT = self.get_species_enthalpies_RT(T)
        s0_R  = self.get_species_entropies_R(T)
        return h0_RT - s0_R

    def get_equilibrium_constants(self, T):
        RT = self.gas_constant * T
        C0 = np.log( self.one_atm / RT )

        g0_RT = self.get_species_gibbs_RT( T )
        return np.array([
            %for react in sol.reactions():
                %if react.reversible:
                    ${cgm(equilibrium_constants_expr(
                        sol, react, Variable("g0_RT")))},
                %else:
                    0*T,
                %endif
            %endfor
            ])

    def get_temperature(self, H_or_E, T_guess, Y, do_energy=False):
        if do_energy == False:
            pv_fun = self.get_mixture_specific_heat_cp_mass
            he_fun = self.get_mixture_enthalpy_mass
        else:
            pv_fun = self.get_mixture_specific_heat_cv_mass
            he_fun = self.get_mixture_internal_energy_mass

        num_iter = 500
        tol = 1.0e-6
        T_i = T_guess
        dT = 1.0
        F  = H_or_E
        J  = 0.0

        for iter in range( 0, num_iter ):
            F    -= he_fun( T_i, Y )
            J    -= pv_fun( T_i, Y )
            dT    = - F / J
            T_i  += dT
            if np.abs( dT ) < tol:
                T = T_i
                break
            F = H_or_E
            J = 0.0

        T = T_i

        return T

""", strict_undefined=True)

# }}}


def gen_python_code(sol: ct.Solution):
    code = code_tpl.render(
        ct=ct,
        sol=sol,

        str_np=str_np,
        cgm=CodeGenerationMapper(),
        Variable=p.Variable,

        poly_to_expr=poly_to_expr,
        poly_to_enthalpy_expr=poly_to_enthalpy_expr,
        poly_to_entropy_expr=poly_to_entropy_expr,
        equilibrium_constants_expr=equilibrium_constants_expr,
    )
    print(code)
    exec_dict = {}
    exec(compile(code, "<generated code>", "exec"), exec_dict)
    return exec_dict["Thermochemistry"]


# vim: foldmethod=marker
