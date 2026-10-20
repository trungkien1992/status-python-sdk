import pandas as pd
import datetime, requests, eth_abi, re
from . import exceptions
from .utils import misc as utils
from .saved_address import SavedAddress
from typing import TYPE_CHECKING, Union, Optional

if TYPE_CHECKING:
    from .account import Account

class Wallet:
    __ACTIVE_CHAINS = 5
    __keccak256_selectors = {
        "transfer": "a9059cbb" # keccak256("transfer(address,uint256)")[:4]
    }
    def __init__(self, account: "Account"):
        self.__account = account
        # Available Status App networks
        self.__available_chains = pd.DataFrame([
            {
                "id": info["chainId"],
                "name": info["chainName"],
                "short_name": info["shortName"],
                "layer": info["layer"],
                "explorer_url": info["blockExplorerUrl"],
                "native_decimals": info["nativeCurrencyDecimals"],
                "native_symbol": info["nativeCurrencySymbol"],
                "type": "Mainnet" if not info["isTest"] else "Testnet"
            }
            for info in self.__account._call_rpc("network", "getFlatEthereumChains").get("result", [])
        ]).sort_values("id", ascending=True).reset_index(drop=True)

        # Available Status App tokens
        columns = ["chainId", "address", "symbol", "decimals", "crossChainId"]
        info = []
        result: list[dict] = self.__account._call_rpc("wallets", "getAllTokenLists").get("result", [])
        for current in result:
            if len(current["tokens"]) == 0:
                continue
            data = pd.DataFrame(current["tokens"])[columns]
            data = data.assign(
                decimals = data["decimals"].astype("int8"),
                chainId = data["chainId"].astype("int8"),
                crossChainId = data["crossChainId"].apply(lambda value: None if len(value) == 0 else value),
                source_id = current["name"]
            )
            info.append(data)

        self.__available_tokens = pd.concat(info, ignore_index=True)
        self.__available_tokens.columns = [utils.camel_to_snake(column) for column in self.__available_tokens.columns]

        # Available Status App currencies
        self.__iso4217_ccy = [
            ccy.upper()
            for ccy in self.__account._call_rpc("wallets", "getCachedCurrencyFormats").get("result", {}).keys()
            if len(ccy) == 3 and ccy.upper() != "XXX"
        ]

    @property
    def saved_addresses(self) -> dict[str, SavedAddress]:
        """
        Get current saved addresses
        """
        addresses = {
            current["address"].lower(): SavedAddress(current, self.__account._call_rpc)
            for current in self.__get_raw_saved_addresses()
        }
        return addresses

    @property
    def available_tokens(self) -> pd.DataFrame:
        """
        Tokens available in Status App
        """
        return self.__available_tokens

    @property
    def available_chains(self) -> pd.DataFrame:
        """
        Networks available in Status App
        """
        return self.__available_chains

    def get_current_balance(self) -> pd.DataFrame:
        """
        Get the account's current balance
        """
        empty = pd.DataFrame(columns=["timestamp", "address", "chain_id", "amount", "symbol"])

        params = [[self.__account.info["wallet_address"]], True]
        results = self.__account._call_rpc("wallets", "fetchOrGetCachedWalletBalances", params).get("result", {}).get(self.__account.info["wallet_address"].lower(), [])
        if not results:
            return empty.copy()

        balance = pd.DataFrame(results)
        column_mapping = {"tokenAddress": "address", "tokenChainId": "chain_id", "balance": "amount", "hasError": "error"}
        balance = balance.rename(columns=column_mapping)[list(column_mapping.values())]\
                        .astype({"chain_id": "int8", "amount": "float64"})

        query = (balance["amount"] != 0) & (~balance["error"])
        if query.sum() == 0:
            return empty.copy()

        redundant_columns = ["error", "decimals", "cross_chain_id", "source_id"]
        # available_tokens = self
        balance = balance.loc[query].merge(self.available_tokens, "left", ["address", "chain_id"])\
                                    .drop(redundant_columns, axis=1)\
                                    .drop_duplicates()\
                                    .sort_values("chain_id", ascending=True)\
                                    .reset_index(drop=True)\

        balance.insert(0, "timestamp", datetime.datetime.now())
        return balance.copy()

    def search(self, token_addresses: Union[list[str], str], chain_ids: Union[list[int], int] = 1, wallets: Optional[Union[list[str], str]] = None, ccy: Optional[Union[str, list[str]]] = None) -> pd.DataFrame:
        """
        Get the current amount for the provided token addresses, chain IDs and wallets.

        Parameters:
            - `token_addresses` - the token addresses as they appear in `get_tokens()`
            - `chain_ids` - chain IDs as they appear in `self.chains`
            - `wallets` - if left blank, the account's `wallet_address` will be used. However other wallets can be monitored as well.
            - `ccy` - the fiat currency the tokens will be converted to.

        Output:
            - DataFrame containing the current balance and fiat amount (if `ccy` is provided)
        """

        if isinstance(wallets, str):
            wallets = [wallets]

        elif isinstance(wallets, type(None)):
            wallets = [self.__account.info["wallet_address"]]

        if isinstance(wallets, list):
            wallets = list(set(wallets))

        if isinstance(token_addresses, str):
            token_addresses = [token_addresses]

        if isinstance(token_addresses, list):
            token_addresses = list(set(token_addresses))

        if isinstance(chain_ids, int):
            chain_ids = [chain_ids]

        if isinstance(ccy, str):
            ccy = [ccy]
        elif isinstance(ccy, type(None)):
            ccy = []

        if ccy:
            ccy = [
                current.upper()
                for current in ccy
                if current.upper() in self.__iso4217_ccy
            ]

        tokens = self.__get_valid_tokens(chain_ids, token_addresses)
        result: dict[str, dict[str, dict[str, str]]] = self.__account._call_rpc("wallets", "getBalancesByChain", [wallets, tokens]).get("result", {})
        data = [
            {
                "chain_id": int(chain_id),
                "wallet_address": wallet_address,
                "token_address": token_address,
                "amount": token_hex_amount,
            }
            for chain_id, chain_info in result.items()
            for wallet_address, wallet_info in chain_info.items()
            for token_address, token_hex_amount in wallet_info.items()
        ]

        if not data:
            return pd.DataFrame()

        data = pd.DataFrame(data)
        available_tokens = self.available_tokens
        column_mapping = {"chain_id": "chain_id", "address": "token_address", "symbol": "token_symbol", "decimals": "decimals"}
        data: pd.DataFrame = data.merge(
            available_tokens[list(column_mapping.keys())].rename(columns=column_mapping).drop_duplicates(),
            "left",
            ["chain_id", "token_address"]
        )
        final_columns = ["timestamp", "wallet_address", "token_address", "token_symbol", "amount", "chain_id"]
        data = data.assign(
            timestamp = datetime.datetime.now(),
            chain_id = data["chain_id"].astype("int8"),
            amount = data.apply(lambda row: int(row["amount"], 16) / (10 ** row["decimals"]), axis=1)
        )[final_columns]

        query = data["amount"] > 0
        data = data.loc[query].reset_index(drop=True)
        if not ccy or len(data) == 0:
            return data.copy()

        tokens = (data["chain_id"].astype(str) + "-" + data["token_address"]).to_list()
        result = self.__account._call_rpc("wallets", "fetchPrices", [tokens, ccy]).get("result", {})
        if not result:
            return data.copy()

        rates = pd.DataFrame([
            {
                "chain_id": int(address.split("-")[0]),
                "token_address": address.split("-")[1],
                "ccy": ccy,
                "price": price
            }
            for address, prices in result.items()
            for ccy, price in prices.items()
        ])
        data = data.merge(rates, "outer", ["chain_id", "token_address"])
        data["fiat_value"] = data["amount"] * data["price"]
        return data

    def get_current_market_info(self, token_addresses: Union[list[str], str], chain_ids: Union[list[int], int] = 1, ccy: str = "USD") -> pd.DataFrame:
        """
        Get market information for the provided token addresses and chain IDs.

        Parameters:
            - `token_addresses` - the token addresses as they appear in `get_tokens()`
            - `chain_ids` - chain IDs as they appear in `self.chains`
            - `ccy` - the fiat currency the market values will be fetched for.

        Output:
            - DataFrame containing the current market values and fiat amount
        """
        if isinstance(token_addresses, str):
            token_addresses = [token_addresses]

        if isinstance(token_addresses, list):
            token_addresses = list(set(token_addresses))

        if isinstance(chain_ids, int):
            chain_ids = [chain_ids]

        ccy = ccy.upper()
        if ccy not in self.__iso4217_ccy:
            raise exceptions.InvalidCurrencyError(f"Given currency {ccy} is invalid...\nAvailable ISO 4217 currencies: {self.__iso4217_ccy}")

        tokens = self.__get_valid_tokens(chain_ids, token_addresses)
        market_info = pd.DataFrame([
            {
                "chain_id": int(token_address.split("-")[0]),
                "address": token_address.split("-")[1],
                "currency": ccy,
                **info
            }
            for token_address, info in self.__account._call_rpc("wallets", "fetchMarketValues", [tokens, ccy]).get("result", {}).items()
        ])
        market_info: pd.DataFrame = market_info.assign(
            timestamp = datetime.datetime.now(),
            chain_id = market_info["chain_id"].astype("int8")
        )
        market_info = market_info.merge(
            self.available_tokens[["chain_id", "address", "symbol"]].drop_duplicates(),
            "left",
            ["chain_id", "address"]
        )
        column_mapping = {
            "timestamp": "timestamp",
            "chain_id": "chain_id",
            "address": "token_address",
            "symbol": "token_symbol",
            "currency": "fiat_ccy",
            "MKTCAP": "market_cap",
            "HIGHDAY": "high_price",
            "LOWDAY": "low_price",
            "CHANGE24HOUR": "pnl_24hr",
            "CHANGEPCTDAY": "pct_change",
            "CHANGEPCTHOUR": "pct_change_1hr",
            "CHANGEPCT24HOUR": "pct_change_24hr"
        }
        market_info = market_info.rename(columns=column_mapping)[list(column_mapping.values())]
        return market_info.copy()

    def send_transaction(self, address: str, symbol: str, amount: float, chain_id: int = 1) -> Optional[str]:
        """
        Send crypto to specified `address`

        Parameters:
            - `address` - the wallet address of the receiver
            - `symbol` - either a valid Status token symbol from `available_tokens` or its address
            - `amount` - the amount that will be sent to the `address`
            - `chain_id` - valid Chain from `self.chains`

        Output:
            - Transaction hash that to monitor the transactions progress
        """
        def verify_chain_id(chain_id: int) -> int:
            if chain_id not in self.available_chains["id"].to_list():
                raise exceptions.InvalidChainIdError(f"Given chain ID {chain_id} was not found! Please check `available_chains` for available options...")

            return chain_id

        is_eth = symbol == "ETH"
        is_address = symbol.startswith("0x")
        symbol = symbol.upper()
        chain_id = verify_chain_id(chain_id)

        tokens = self.available_tokens[["chain_id", "address", "symbol", "decimals"]].drop_duplicates().reset_index(drop=True)
        query = (tokens["address" if is_address else "symbol"] == symbol) & (tokens["chain_id"] == chain_id)
        if query.sum() == 0:
            raise exceptions.InvalidTokenError(f"Given {'address' if is_address else 'symbol'} {symbol} on chain ID {chain_id} does not exist...")

        if query.sum() > 1 and not is_address:
            results = "\n- ".join(tokens.loc[query, "address"].to_list())
            raise exceptions.InvalidTokenError(f"Found {query.sum()} addresses for token {symbol}:\n- {results}\n\nPlease provide the token address instead...")

        token_info = tokens.loc[query].to_dict("records")[0]

        balance = self.get_current_balance()
        query = (balance["address"] == token_info["address"]) & (balance["chain_id"] == chain_id)
        if query.sum() == 0:
            raise exceptions.InvalidTokenError(f"Given {'address' if is_address else 'symbol'} {symbol} on chain ID {chain_id} was not found in your wallet ({self.__account.info['wallet_address']})...")

        wallet_amount = balance.loc[query].reset_index(drop=True)["amount"].iloc[0]
        if amount > wallet_amount:
            raise exceptions.InvalidTokenError(f"Given {'address' if is_address else 'symbol'} {symbol} on chain ID {chain_id} has {wallet_amount} but you are trying to send {amount}...")

        raw_amount = int(amount * (10**token_info["decimals"]))

        tx = {
            "version": 1,
            "from": self.__account.info["wallet_address"],
            "to": address if is_eth else token_info["address"],
            "value": hex(raw_amount) if is_eth else "0x0",
            "fromChainID": chain_id,
            "toChainID": chain_id,
        }
        if not is_eth:
            encoded_args = eth_abi.encode(["address", "uint256"], [address, raw_amount]).hex()
            tx["data"] = "0x" + self.__keccak256_selectors["transfer"] + encoded_args

        payload = {
            "password": self.__account.info["password"],
            "txArgs": tx
        }
        response = requests.post(self.__account._urls["http"]["transaction"], json=payload)
        transaction_hash: str = response.json().get("result")
        chain_url = self.available_chains.set_index("id")["explorer_url"].to_dict().get(chain_id)
        url = f"{chain_url}/tx/{transaction_hash}"
        self.__account.logger.info(f"Transaction: {url}")
        return transaction_hash

    def __getitem__(self, key: str) -> pd.DataFrame:
        """
        Get the fiat currency balance
        """
        ccy = key.upper()

        if ccy not in self.__iso4217_ccy:
            raise exceptions.InvalidCurrencyError(f"{ccy} is an invalid fiat (ISO 4217) currency code...")

        balance = self.get_current_balance()
        tokens = (balance["chain_id"].astype(str) + "-" + balance["address"]).to_list()
        result = self.__account._call_rpc("wallets", "fetchPrices", [tokens, [ccy]]).get("result", {})
        if result:
            rates = pd.DataFrame([
                {
                    "chain_id": int(address.split("-")[0]),
                    "address": address.split("-")[1],
                    "rate": price,
                    "ccy": ccy,
                }
                for address, prices in result.items()
                for ccy, price in prices.items()
            ])
            balance = balance.merge(rates, "left", ["chain_id", "address"])
            balance["fiat_value"] = balance["amount"] * balance["rate"]
        else:
            balance = balance.assign(
                rate = None,
                ccy = ccy,
                fiat_value = None,
            )

        return balance.copy()

    def __get_valid_tokens(self, chain_ids: list[int], token_addresses: list[str]) -> list[str]:
        """
        Convert the given tokens to `status-go` RPC format

        Parameters:
            - `chain_ids` - selected chain IDs
            - `token_addresses` - can be a mix of token addresses and token symbols

        Output:
            - list of `{chain_id}-{token_address}`
        """
        available_chains = self.available_chains["id"].to_list()
        tokens = []
        for chain_id in chain_ids:

            if chain_id not in available_chains:
                continue

            query = ((self.available_tokens["address"].isin(token_addresses)) | (self.available_tokens["symbol"].isin(token_addresses))) & (self.available_tokens["chain_id"] == chain_id)
            selected = self.available_tokens.loc[query].reset_index(drop=True).copy()
            if len(selected) == 0:
                continue
            tokens += selected.apply(lambda row: f"{row['chain_id']}-{row['address']}", axis=1).unique().tolist()

        return tokens

    def add_address(self, address: str, name: str, ens: Optional[str] = None):
        """
        Save a wallet address, so it can be reused instead of being typed out every time.
        Saved addresses can be found in `saved_addresses`.

        Parameters:
            - `address` - the wallet address to save
            - `name` - the name to save the address under. Must follow the Status App naming rules
            - `ens` - the ENS name of the address
        """
        if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            raise exceptions.SavedAddressError(f"'{address}' is not a valid wallet address - it must be '0x' followed by 40 hexadecimal characters...")

        SavedAddress.validate_name(name)
        if not ens:
            ens = ""

        address = address.lower()
        saved = self.saved_addresses
        if address in saved:
            raise exceptions.SavedAddressError(f"'{address}' is already saved as '{saved[address].name}'. Please edit it through `saved_addresses` instead...")

        params = [{
            "address": address,
            "name": name,
            "ens": ens,
            "isTest": False
        }]
        response = self.__account._call_rpc("messaging", "upsertSavedAddress", params)
        error = response.get("error", {}) or {}
        if error:
            raise exceptions.SavedAddressError(error.get("message"))

    def __get_raw_saved_addresses(self) -> list[dict]:
        """
        Get currently Saved Addresses
        """
        response = self.__account._call_rpc("messaging", "getSavedAddresses")
        error = response.get("error", {}) or {}
        if error:
            raise exceptions.BackendError(error.get("message"))

        return response["result"] or []

    @property
    def selected_chains(self) -> pd.DataFrame:
        """
        Get the currently enabled chains
        """
        current = [
            {
                "id": info["chainId"],
                "name": info["chainName"],
                "short_name": info["shortName"],
                "layer": info["layer"],
                "explorer_url": info["blockExplorerUrl"],
                "native_decimals": info["nativeCurrencyDecimals"],
                "native_symbol": info["nativeCurrencySymbol"],
                "type": "Mainnet" if not info["isTest"] else "Testnet"
            }
            for info in self.__account._call_rpc("network", "getFlatEthereumChains").get("result", [])
            if info["isActive"]
        ]
        return pd.DataFrame(current)


    def activate_chain(self, chain_id: int):
        """
        Activate a network, so it can be used by the wallet.

        You can have up to 5 active `Mainnet` and 5 active `Testnet` networks at the same time.
        The two categories are counted separately, so activating a `Testnet` is not blocked by
        having 5 active `Mainnet` networks. Deactivate a network of the same category first to
        make room.

        Parameters:
            - `chain_id` - a chain ID from `available_chains`
        """
        categories: dict[int, str] = self.available_chains.set_index("id")["type"].to_dict()
        if chain_id not in categories:
            raise exceptions.InvalidChainIdError(f"Given chain ID {chain_id} was not found! Please check `available_chains` for available options...")

        category = categories[chain_id]
        selected = self.selected_chains
        active: list[int] = [] if selected.empty else selected.loc[selected["type"] == category, "id"].to_list()
        if chain_id in active:
            return

        if len(active) >= self.__ACTIVE_CHAINS:
            raise exceptions.ActiveChainLimitError(f"{category} already has {len(active)} active chains ({', '.join(str(current) for current in active)}). Status App allows up to {self.__ACTIVE_CHAINS} per category - please deactivate one first...")

        response = self.__account._call_rpc("network", "setChainActive", [chain_id, True])
        error = response.get("error", {}) or {}
        if error:
            raise exceptions.ActiveChainLimitError(error.get("message"))

    def deactivate_chain(self, chain_id: int):
        """
        Deactivate a network, so the wallet stops using it.
        Active networks are currently can be found in `selected_chains`.

        Parameters:
            - `chain_id` - a chain ID from `selected_chains`
        """
        if chain_id not in self.available_chains["id"].to_list():
            raise exceptions.InvalidChainIdError(f"Given chain ID {chain_id} was not found! Please check `available_chains` for available options...")

        selected = self.selected_chains
        active: list[int] = [] if selected.empty else selected["id"].to_list()
        if chain_id not in active:
            return

        response = self.__account._call_rpc("network", "setChainActive", [chain_id, False])
        error = response.get("error", {}) or {}
        if error:
            raise exceptions.ChainDeactivationError(error.get("message"))
