from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Callable


class RegistroTexto:
    """
    Registro persistente simples, com um valor por linha.

    O arquivo funciona como estado/cache local:
    - leitura em memória para consulta rápida;
    - evita duplicidade;
    - grava cada novo valor imediatamente;
    - flush + fsync reduzem o risco de perder o último registro
      caso o processo seja interrompido logo após a gravação.
    """

    def __init__(
        self,
        caminho: Path,
        normalizar: Callable[[object], str] | None = None,
    ) -> None:
        self.caminho = Path(caminho)
        self.normalizar = normalizar or (
            lambda valor: str(valor).strip()
        )
        self._lock = Lock()
        self._valores = self._carregar()

    def _normalizar(
        self,
        valor: object,
    ) -> str:
        return self.normalizar(valor).strip()

    def _carregar(self) -> set[str]:
        if not self.caminho.exists():
            return set()

        valores: set[str] = set()

        with self.caminho.open(
            "r",
            encoding="utf-8-sig",
        ) as arquivo:
            for linha in arquivo:
                valor = self._normalizar(
                    linha
                )

                if valor:
                    valores.add(valor)

        return valores

    def __len__(self) -> int:
        return len(self._valores)

    def contem(
        self,
        valor: object,
    ) -> bool:
        normalizado = self._normalizar(
            valor
        )

        if not normalizado:
            return False

        return normalizado in self._valores

    def adicionar(
        self,
        valor: object,
    ) -> bool:
        normalizado = self._normalizar(
            valor
        )

        if not normalizado:
            return False

        with self._lock:
            if normalizado in self._valores:
                return False

            self.caminho.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with self.caminho.open(
                "a",
                encoding="utf-8",
                newline="\n",
            ) as arquivo:
                arquivo.write(
                    normalizado + "\n"
                )
                arquivo.flush()
                os.fsync(
                    arquivo.fileno()
                )

            self._valores.add(
                normalizado
            )

            return True
