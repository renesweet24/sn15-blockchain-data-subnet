from neurons.miners.configs import GraphDatabaseConfig
from neo4j import GraphDatabase


class GraphIndexer:
    def __init__(self, config: GraphDatabaseConfig):
        self.driver = GraphDatabase.driver(
            config.graph_db_url,
            auth=(config.graph_db_user, config.graph_db_password),
        )

    def close(self):
        self.driver.close()

    def get_latest_block_number(self):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (t:Transaction)
                RETURN MAX(t.block_height) AS latest_block_height
                """
            )
            single_result = result.single()
            if single_result[0] is None:
                return 0
            return single_result[0]

    from decimal import getcontext

    # Set the precision high enough to handle satoshis for Bitcoin transactions
    getcontext().prec = 28

    def create_indexes(self):
        with self.driver.session() as session:
            index_creation_statements = [
                "CREATE INDEX ON :Transaction(tx_id);",
                "CREATE INDEX ON :Transaction(block_height);",
                "CREATE INDEX ON :Address(address);",
                "CREATE INDEX ON :SENT(value_satoshi)",
            ]
            for statement in index_creation_statements:
                try:
                    session.run(statement)
                except Exception as e:
                    print(f"An exception occurred: {e}")

    def create_graph_focused_on_money_flow_experimental(self, in_memory_graph):
        block_node = in_memory_graph["block"]
        transactions = block_node.transactions

        with self.driver.session() as session:
            try:
                # Process all transactions in a single batch
                session.run(
                    """
                    UNWIND $transactions AS tx
                    MERGE (t:Transaction {tx_id: tx.tx_id})
                    ON CREATE SET t.timestamp = tx.timestamp,
                                  t.block_height = tx.block_height,
                                  t.is_coinbase = tx.is_coinbase
                    """,
                    transactions=[
                        {
                            "tx_id": tx.tx_id,
                            "timestamp": tx.timestamp,
                            "block_height": tx.block_height,
                            "is_coinbase": tx.is_coinbase,
                        }
                        for tx in transactions
                    ],
                )

                # Process all vouts in a single batch
                vouts = []
                for tx in transactions:
                    for index, vout in enumerate(tx.vouts):
                        vouts.append(
                            {
                                "tx_id": tx.tx_id,
                                "address": vout.address,
                                "value_satoshi": vout.value_satoshi,
                                "is_coinbase": tx.is_coinbase
                                and index
                                == 0,  # True only for the first vout of a coinbase transaction
                            }
                        )

                session.run(
                    """
                    UNWIND $vouts AS vout
                    MERGE (a:Address {address: vout.address})
                    MERGE (t:Transaction {tx_id: vout.tx_id})
                    CREATE (t)-[:SENT { value_satoshi: vout.value_satoshi, is_coinbase: vout.is_coinbase }]->(a)
                    """,
                    vouts=vouts,
                )

                return True

            except Exception as e:
                print(f"An exception occurred: {e}")
                return False

    def create_graph_focused_on_money_flow(self, in_memory_graph):
        block_node = in_memory_graph["block"]

        with self.driver.session() as session_initial:
            session = session_initial.begin_transaction()
            try:
                for tx in block_node.transactions:
                    # Add the Transaction node
                    session.run(
                        """
                            MERGE (t:Transaction {tx_id: $tx_id})
                            ON CREATE SET t.timestamp = $timestamp,
                                          t.block_height = $block_height,
                                          t.is_coinbase = $is_coinbase
                            """,
                        tx_id=tx.tx_id,
                        timestamp=tx.timestamp,
                        block_height=tx.block_height,
                        is_coinbase=tx.is_coinbase,
                    )

                    if tx.is_coinbase:
                        coinbase_vout = tx.vouts[0]
                        session.run(
                            """
                                MERGE (a:Address {address: $address})
                                MERGE (t:Transaction {tx_id: $tx_id})
                                CREATE (t)-[:SENT {value_satoshi: $value_satoshi, is_coinbase: true }]->(a)
                                """,
                            tx_id=tx.tx_id,
                            address=coinbase_vout.address,
                            value_satoshi=coinbase_vout.value_satoshi,
                        )

                    for vout in tx.vouts:
                        session.run(
                            """
                                MERGE (a:Address {address: $address})
                                MERGE (t:Transaction {tx_id: $tx_id})
                                CREATE (t)-[:SENT { value_satoshi: $value_satoshi, is_coinbase: false }]->(a)
                                """,
                            tx_id=tx.tx_id,
                            address=vout.address,
                            value_satoshi=vout.value_satoshi,
                        )

                session.commit()
                return True

            except Exception as e:
                session.rollback()  # Roll back the transaction if there's an error
                print(f"An exception occurred: {e}")
                return False
            finally:
                session.close()  # Close the session
